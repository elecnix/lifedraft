#!/usr/bin/env python3
"""
Simulation Configuration and Result data structures.

Extracted from simulation.py to break circular imports between core
and jurisdiction modules.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple
from copy import deepcopy
from pathlib import Path

from return_model import ReturnModel, ReturnEngine
# DP#25 (issue #998): find_member_by_role / adult_members / projection_span
# are pure config-reading helpers (they read the raw family.members list and
# derive structural facts with NO simulation machinery). They belong in the
# data layer (member_config), not here -- keeping them here forced the scenario
# layer (scenario_discovery) to import from this simulation-layer module to
# reach them, a DP#25 outward dependency. Relocated to member_config; imported
# back here (simulation -> data, inward) so SimulationConfig.member_by_role /
# .adult_members and the projection_years derivation keep their single spelling
# (DP#9 -- no shim, no re-export: callers that used to import these from
# simulation_config now import them from member_config directly).
from member_config import find_member_by_role, adult_members, projection_span

logger = logging.getLogger(__name__)


# ── Issue #664: the charge as a first-class concept ─────────────────────────
#
# On a readvanceable/all-in-one mortgage (Manulife One, Scotia STEP, BNC
# All-in-One), the amortizing mortgage and the revolving HELOC are carved out
# of ONE registered charge against the property with ONE combined limit. They
# are not independent borrowing sources -- paying the mortgage down is what
# *creates* HELOC room; drawing the HELOC is what *consumes* it.
#
# OSFI's "Clarification on the Treatment of Innovative Real Estate Secured
# Lending Products under Guideline B-20" (Combined Loan Plan guidance,
# osfi-bsif.gc.ca) states: the overall CLP limit (amortizing + revolving)
# cannot exceed 80% LTV -- the legal maximum for an uninsured mortgage. Any
# lending above 65% LTV must be amortizing and non-readvanceable; principal
# payments applied to the segment above 65% must be matched by a reduction in
# the overall authorized limit, until the CLP's combined limit is back down to
# 65% LTV. In other words: the REVOLVING/readvanceable segment on its own is
# capped at 65% LTV, independent of the 80% combined cap.
OSFI_B20_CHARGE_LTV_MAX = 0.80
OSFI_B20_REVOLVING_LTV_MAX = 0.65

# Issue #1075 (optimizer half): the DEFAULT floor for the house tranche's
# sweep -- 60% of the registered charge -- when the structure declares no
# ``min_house_floor``. DP#13: a fallback for ABSENT input, never an opinion
# that overrides a declared floor (``_tranche_house_floor`` prefers the
# declared value, and the sweep's floor is not the cash-back threshold).
_HOUSE_SWEEP_FLOOR_FRACTION = 0.6

# Dollar tolerance for charge-limit comparisons (floating-point rounding, not
# a policy margin).
_CHARGE_TOLERANCE = 0.01


def charge_limit(house_value: float, charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX) -> float:
    """The combined-facility ceiling of ONE registered charge against a
    property (issue #664): mortgage balance + drawn/limit revolving HELOC
    must fit inside this. ``charge_ltv_limit`` is a fallback (DP#13), not an
    opinion -- pass a config's own declared ``charge_ltv_limit`` when one is
    known (e.g. ``charge_limit(config.house_value, config.charge_ltv_limit)``).
    """
    return house_value * charge_ltv_limit


def heloc_revolving_limit(house_value: float, heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX) -> float:
    """The revolving-only ceiling (OSFI B-20): independent of the 80%
    combined cap, the readvanceable/revolving segment alone may not exceed
    65% LTV -- lending between 65% and 80% must be amortizing and
    non-readvanceable.
    """
    return house_value * heloc_ltv_limit


def charge_room_for_readvance(house_value: float,
                               mortgage_balance: float,
                               drawn_revolving: float,
                               charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX,
                               heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX) -> float:
    """How much a readvanceable line may advance right now, given the
    charge it shares with the mortgage (issue #681).

    This is THE readvanceable-mortgage mechanism, stated once (DP#7: the
    mechanism, not the branded product; DP#9: once, not per caller). The
    charge registered against the property is FIXED, so paying mortgage
    principal down is what CREATES line room, and drawing the line is what
    CONSUMES it. The line cannot advance past the charge -- when the charge
    is full, the readvance stops (returns 0.0, never negative).

    Two ceilings bind, whichever is lower (OSFI B-20, see the module
    constants):

      - the COMBINED charge (80% LTV): the amortizing mortgage plus the
        drawn revolving balance must fit inside ``charge_limit``;
      - the REVOLVING-only ceiling (65% LTV): the drawn revolving balance
        alone must fit inside ``heloc_revolving_limit``, independent of how
        much combined room the 80% cap leaves -- lending between 65% and 80%
        must be amortizing and non-readvanceable, i.e. it cannot be
        readvanced into the line at all.

    Pure function (DP#3): same inputs, same output; no state, no clamping of
    its own inputs.

    Args:
        house_value: the appraised value the charge is registered against.
        mortgage_balance: the amortizing segment's balance right now.
        drawn_revolving: the revolving segment's DRAWN balance right now
            (SM-readvanced + any personal-draw margin) -- not its limit.
        charge_ltv_limit: combined ceiling as a fraction of house_value.
        heloc_ltv_limit: revolving-only ceiling as a fraction of house_value.

    Returns:
        The advanceable room, in [0, inf).
    """
    combined_room = charge_limit(house_value, charge_ltv_limit) - (mortgage_balance + drawn_revolving)
    revolving_room = heloc_revolving_limit(house_value, heloc_ltv_limit) - drawn_revolving
    return max(0.0, min(combined_room, revolving_room))


class ChargeLimitExceededError(ValueError):
    """DP#32 (issue #664): total secured debt against a property's single
    registered charge exceeds the charge's LTV ceiling. On a readvanceable/
    all-in-one mortgage the mortgage and the HELOC share ONE charge and are
    NOT independent borrowing sources -- refused loudly rather than silently
    modeled as a >100% LTV facility.
    """


class MissingRefinanceAmortizationError(ValueError):
    """DP#32 (issue #655): a cash-out refinance is a NEW LOAN with its own
    amortization. The overlay refuses to silently inherit the incumbent
    mortgage's remaining amortization when no refinance amortization is
    declared or supplied -- doing so overstates the required payment by
    roughly 2x on a near-payoff mortgage.
    """


class ReadvanceableWithoutPropertyError(ValueError):
    """DP#32 (issue #681/#657): the readvanceable-mortgage strategy is a claim
    on a charge registered against a PROPERTY, but no property value was
    declared, so the line's advanceable room is unknowable -- refused loudly.

    This is a TYPED infeasibility, not a bug: when the optimizer's grid sweeps
    ``use_readvanceable=[True, False]`` over a household with no property, the
    ``True`` branch is a legitimately-infeasible sweep point (like an LTV the
    property cannot support). Issue #657: the optimizer catches THIS narrowly
    and reports it as ``is_infeasible`` with a reason, rather than crashing the
    whole run OR swallowing it into a silent ``-inf`` row. A subclass of
    ValueError so the direct FamilySimulation path still fails loud for callers
    that hand-build an impossible config.
    """


def _dict_to_json(data: Dict, path: str = None, indent: int = 2) -> str:
    """Pure-ish: serialize a dict to a JSON string, optionally writing it to
    disk (DP#24).

    DP#9: the one implementation of "to_json = dump to_dict(), optionally
    persist to path". Shared by ScenarioOverlay.to_json and
    SimulationConfig.to_json (found byte-identical, similarity 1.00, by
    dupdelta, the repo's clone detector). ScenarioOverlay previously also carried a second,
    older to_json() with a different signature (no path/indent) that was
    silently shadowed and unreachable — dead code from a duplicate method
    definition; removed.
    """
    json_str = json.dumps(data, indent=indent, ensure_ascii=False)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json_str)
    return json_str


# ── Issue #596/#602 (epic #603): reject an unknown/typo'd key instead of
# silently dropping it, in SimulationConfig's INTERNAL dict shape.
#
# NOTE what this is and is not (epic #603 Track C Phase 2b): this is NOT the
# input contract's wire-format validation -- that is real, full
# ``additionalProperties: false`` JSON Schema validation, done once, at the
# ONE loading boundary (``contract_schema.validate_contract``, called from
# ``SimulationConfig.from_json`` / every CLI script's ``--input`` flag via
# ``input_contract.load_and_map``). This guard covers a DIFFERENT, narrower
# thing: ``SimulationConfig.from_dict`` also doubles as the internal
# constructor ``apply_overlay``/``ScenarioOverlay``/the optimizer's
# scenario-generation machinery rebuilds a ``SimulationConfig`` from
# (DP#18/DP#24/DP#25 -- an in-memory working representation used throughout
# the simulation/optimization layers, not a second wire format nothing
# outside this file and ``input_contract.py`` ever authors from scratch on
# disk). That internal shape predates a real schema and is exercised by
# ~4,169 tests with raw dicts, so going fully strict here risks breaking a
# large, untriaged set of them. What IS safe and real: the ROOT-level key
# set is a closed, well-known list (every key any production reader ever
# fetches off the raw config dict, verified by grepping every
# ``cfg.get('...')``/``config.get('...')`` call site across the whole
# first-party source tree, not just this file) and the `family` object's
# own two keys (`members`/`children`) are equally well-established -- so
# both get real enforcement. Everything nested inside those two keys, and
# every OTHER top-level block's internals (portfolio/heloc/retirement/
# scenarios/...), stays permissive (test_schema_coverage.py's
# DEAD_ALLOWLIST/CONSUMED registries mechanically enumerate what actually
# reaches a decision in that internal shape).
_INTERNAL_ROOT_ALLOWED_KEYS = {
    'accounts', 'assumptions', 'cash_flows', 'country', 'employer_benefits',
    # epic #603 Track C Phase 2c (#600): the estate elections block.
    'estate',
    'family', 'heloc',
    # issue #679: household's own measured living-cost budget + emergency
    # reserve (facts, not the assumptions.savings_rate belief).
    'household_budget',
    'life_events', 'lira', 'lsif', 'market_rates',
    'portfolio', 'portfolio_composition', 'prescribed_rate_loan',
    'private_corp_dividends', 'property', 'rate_scenarios', 'rental_income',
    'rental_properties', 'retirement', 'retirement_target', 'return_model',
    'savings', 'scenarios', 'sensitivity_overlays', 'sensitivity_overlay_presets',
    # issue #713: the household's OWN authored contribution strategies
    # (decisions.contribution_strategy[]), mapped into the shape
    # AllocationStrategy.from_dict reads and handed to the optimizer's
    # discover_strategies(custom_strategies=...) search space. Read by
    # optimize.py, not by SimulationConfig itself -- the optimizer, not the
    # simulator, is what ranks strategies (DP#22/DP#30).
    'strategies',
    # issue #763: closed-end consumer liabilities (car_loan/student_loan/
    # personal_loan) -- a first-class amortizing-debt list the engine
    # services alongside the mortgage (SimulationConfig.consumer_loans ->
    # simulation_rules.apply_consumer_loans -> apply_solvency debt service).
    'consumer_loans',
    # issue #759: fixed-term, zero-interest installment obligations -- a
    # first-class committed-payment list the engine services alongside the
    # mortgage + consumer loans (SimulationConfig.installments ->
    # simulation_rules.apply_installments -> apply_solvency debt service).
    # NOT a callable debt (excluded from total_debt); the payment is the
    # non-discretionary outflow, the balance is the remaining scheduled
    # payments (a reporting figure, not a balance-sheet liability).
    'installments',
    # Issue #768: record-only equity grants (private stock options). No rule
    # reads this list -- the $0-for-solvency contribution is by construction.
    'equity_grants',
    # Issue #936: deposit products. `deposit_products` is the DECLARED
    # list the optimizer sweeps take-vs-leave (read by scenario_discovery /
    # simulate, like 'strategies'/'objective' -- the optimizer ranks, not the
    # simulator, DP#22). `deposit_product` is the SINGLE taken product apply_overlay
    # writes onto a scenario's config for the engine (SimState.initial carve +
    # simulation_rules.apply_deposit_product_growth). Both absence-safe (DP#32).
    'deposit_products', 'deposit_product',
    # Issue #1036: the DECLARED borrow-to-invest options, read directly by
    # optimize.run_borrow_to_invest_exploration (the optimizer, not the
    # simulator, DP#22). Not lifted onto a SimulationConfig field (a dead
    # surface -- D7); the raw key is the one spelling. Absence-safe (DP#32).
    'borrow_to_invest_options',
    # Issue #692 (epic #690 bite 1): the couple's NON-principal real properties
    # (a cottage, a rental) as a first-class {id, kind, net_equity} list. Their
    # net equity reaches the annual balance sheet (SimulationConfig.properties
    # -> SimState.property_equities -> total_assets); before this seam only the
    # single principal residence's value was carried, dropping every other one.
    'properties',
    # issue #862 (DP#22/DP#5): the household's declared optimization objective
    # name (decisions.objective), carried through so optimize.py can resolve
    # the ObjectiveFunction the ranking is scored under. Read by optimize.py,
    # not by SimulationConfig itself -- the optimizer, not the simulator, ranks
    # strategies (DP#22). A string, validated against objective.OBJECTIVES at
    # the contract boundary (input_contract) so a typo is refused loudly.
    'objective',
    'tax', 'house_comparison', 'jurisdiction',
    # DP#18/DP#24 round-trip metadata (SimulationConfig.to_dict/apply_overlay
    # attach this; a config saved and reloaded must not be rejected for
    # carrying its own provenance).
    '_overlay',
}
_INTERNAL_FAMILY_ALLOWED_KEYS = {'members', 'children', 'private_loans', 'gifts', 'first_home_purchases'}  # #813; #841 bite 3 gifts; #704 first-home purchases


def _validate_internal_shape(cfg: Dict) -> None:
    """Reject an unrecognized root-level key or a typo'd key inside
    ``family`` (#596/#602's Guard 2, scoped per the module-level comment
    above). Raises ``ValueError`` -- never silently drops or defaults."""
    if not isinstance(cfg, dict):
        raise TypeError(f"SimulationConfig.from_dict expects a dict, got {type(cfg).__name__}")
    # `$`-prefixed keys are schema metadata by this repo's established
    # convention (test_schema_coverage.py's leaf walker skips them the same
    # way).
    unknown_root = {k for k in cfg if not k.startswith('$')} - _INTERNAL_ROOT_ALLOWED_KEYS
    if unknown_root:
        raise ValueError(
            f"Unknown top-level config key(s): {sorted(unknown_root)}. "
            f"If this is a real, newly-added field, add it to "
            f"_INTERNAL_ROOT_ALLOWED_KEYS in simulation_config.py."
        )
    family = cfg.get('family')
    if family is not None:
        if not isinstance(family, dict):
            raise TypeError(f"config['family'] must be a dict, got {type(family).__name__}")
        unknown_family = {k for k in family if not k.startswith('$')} - _INTERNAL_FAMILY_ALLOWED_KEYS
        if unknown_family:
            raise ValueError(
                f"Unknown key(s) in config['family']: {sorted(unknown_family)}. "
                f"Expected only {sorted(_INTERNAL_FAMILY_ALLOWED_KEYS)}."
            )
        for key in ('members', 'children', 'private_loans', 'gifts', 'first_home_purchases'):
            if key in family and not isinstance(family[key], list):
                raise TypeError(f"config['family']['{key}'] must be a list, got {type(family[key]).__name__}")
            for item in family.get(key, []):
                if not isinstance(item, dict):
                    raise TypeError(f"config['family']['{key}'] items must be dicts, got {type(item).__name__}")


def _estate_block(cfg: Dict) -> Dict:
    """The config's declared estate elections (epic #603 Phase 2c, #600).

    DP#32: explicit absence-test, not truthiness -- an `estate` key present but
    explicitly null is a malformed declaration, not "absent", and must not be
    silently laundered into an empty dict by an `or`.
    """
    if 'estate' not in cfg:
        return {}
    block = cfg['estate']
    if block is None:
        raise ValueError(
            "config['estate'] is null. Omit the key entirely to run on "
            "objective._UNDECLARED_ESTATE_DEFAULTS (which is DISCLOSED in the "
            "output as `estate_elections_not_declared`), or state the elections."
        )
    if not isinstance(block, dict):
        raise TypeError(f"config['estate'] must be a dict, got {type(block).__name__}")
    return block


def _reserve_cfg(cfg: Dict) -> Dict:
    """The config's declared emergency-reserve policy block (issue #688).

    DP#32: explicit absence-test, not truthiness. An `assumptions
    .emergency_reserve` key present but explicitly null is a malformed
    declaration, not "absent", and must not be laundered into an empty dict
    by an `or` -- a household that means "I hold no reserve" says so with
    `target_months: 0`, which is a different, louder statement than omitting
    the block, and the two must stay distinguishable all the way to the
    output.
    """
    assumptions = cfg.get('assumptions', {}) or {}
    if 'emergency_reserve' not in assumptions:
        return {}
    block = assumptions['emergency_reserve']
    if block is None:
        raise ValueError(
            "assumptions.emergency_reserve is null. Omit the key entirely to "
            "declare that this household holds NO reserve (the ruin report "
            "states that explicitly -- #688/DP#32), or state the policy "
            "(target_months may be 0)."
        )
    if not isinstance(block, dict):
        raise TypeError(
            f"assumptions.emergency_reserve must be a dict, got {type(block).__name__}")
    return block


def _materialize_return_model_data(cfg: Dict) -> Dict:
    """DP#21 (#260): return_model is the single source of truth for the
    investment return. The deprecated scalar ``assumptions.investment_return``
    is a thin load-time shim: when no ``return_model`` block is present, this
    materializes a ``fixed`` model from it, so every caller resolves the return
    rate from one place and no return field is ever silently ignored.
    """
    assumptions = cfg.get('assumptions', {}) or {}
    return_model_data = cfg.get('return_model') or {}
    if not return_model_data and 'investment_return' in assumptions:
        return_model_data = {
            'type': 'fixed',
            'rate': assumptions['investment_return'],
        }
    return return_model_data


def resolve_return_rate(cfg: Dict, year: int = 0, default: float = 0.07) -> float:
    """DP#21: the representative investment return rate for ``cfg`` in a given
    year, read from ``return_model`` (the single source of truth the engine
    consumes), not from the deprecated ``assumptions.investment_return``
    scalar.

    Once an overlay writes into ``return_model`` (see ``set_return_rate``),
    the raw scalar can be stale or entirely absent; any call site that needs a
    single representative rate for a heuristic (strategy discovery, headline
    display) must resolve it here instead of reading
    ``cfg.get('assumptions', {}).get('investment_return', ...)`` directly, or
    it will silently disagree with the simulation engine (issue #591).

    ``default`` (DP#13: a fallback, not an opinion) is returned only when
    neither ``return_model`` nor ``assumptions.investment_return`` is present
    at all -- callers that want a missing return to be visually obvious
    (rather than defaulting to a plausible-looking 7%) may pass ``default=0``.
    """
    return_model_data = _materialize_return_model_data(cfg)
    if not return_model_data:
        return default
    model = ReturnModel.from_dict(return_model_data)
    return ReturnEngine.return_for_year(model, year)


def set_return_rate(cfg: Dict, rate: float) -> Dict:
    """DP#21/DP#9: the single place that writes an overridden/swept investment
    return rate into ``cfg``. Targets ``return_model`` -- the only key the
    simulation engine reads (``simulation.py`` consumes ``return_model_data``;
    the deprecated ``assumptions.investment_return`` scalar is never
    re-materialized once a ``return_model`` block exists, so writing there is
    a silent no-op -- issue #591).

    A fixed base model has its rate replaced directly. A non-fixed base model
    (variable/stochastic/mean_reverting/stressed) has no single scalar rate to
    replace, so a fixed model at the given rate is substituted and the
    substitution is logged -- no silent no-op (issue #260/#591).

    Mutates and returns ``cfg``. Callers are expected to ``deepcopy`` first
    (DP#18: overlays modify a copy, not the original).
    """
    rm = cfg.get('return_model')
    rm_type = rm.get('type', 'fixed') if rm else 'fixed'
    if rm and rm_type == 'fixed':
        rm['rate'] = rate
    elif rm and rm_type != 'fixed':
        logger.warning(
            "Overriding investment_return=%s over non-fixed return_model "
            "type=%r; substituting a fixed return_model at the override rate.",
            rate, rm_type,
        )
        cfg['return_model'] = {'type': 'fixed', 'rate': rate}
    else:
        cfg['return_model'] = {'type': 'fixed', 'rate': rate}
    return cfg


def has_readvanceable_facility(cfg: Dict) -> bool:
    """DP#32: "no HELOC" is a first-class state, not a missing key (#663).

    ``input_contract.py`` only writes ``property.margin_available`` when the
    contract declares a ``kind=heloc`` liability -- a household with a plain
    mortgage and no readvanceable line correctly has NO key there (writing
    ``0`` would be exactly the silent fallback DP#32 forbids: it would be
    indistinguishable from "a HELOC with zero undrawn room").

    Every consumer that needs to know whether a readvanceable facility
    exists at all -- before quoting its rate, its after-tax cost, or
    ranking the readvanceable-mortgage / investment-loan strategy against
    it (DP#7: mechanism, not the branded product name) -- must ask this
    predicate, not ``cfg.get('property', {}).get('margin_available', 0)``.
    The ``.get(..., 0)`` form collapses "no facility" and "a facility with
    zero room" into the same falsy value and silently disables that
    strategy instead of reporting it structurally unavailable.

    Args:
        cfg: A raw config dict (input.json / contract-mapped internal shape)
            with a ``property`` block.

    Returns:
        True iff ``cfg['property']`` declares a ``margin_available`` key.
    """
    return 'margin_available' in cfg.get('property', {})


def resolve_heloc_rate(cfg: Dict, default: Optional[float] = None) -> Optional[float]:
    """Issues #654/#656/#677: the HELOC's own rate, resolved in the ONE
    precedence order every consumer must use -- never a value aliased from
    a DIFFERENT liability's rate (the mortgage's).

    Precedence -- and, since #685, what each tier is ALLOWED to hold:

    1. ``assumptions.heloc_rate`` -- the DECISION channel. An explicit,
       labelled override for a hypothetical scenario (DP#5: an anchor
       DECISION, e.g. "what if the HELOC also reprices on renewal" --
       ``optimize.py``'s ``apply_anchor_preset`` writes here for a
       *discovered* anchor, and hand-built test/scenario configs may set it
       directly). Real, intentional input -- not a silent default.

       **A BELIEF MAY NOT BE WRITTEN HERE (#685).** This tier outranks the
       household's own signed rate, which is correct for a decision the user
       deliberately posed and catastrophic for anything else. Until #685,
       ``input_contract.to_internal_config`` piped the
       ``assumptions.rate_paths.heloc`` *belief* through this channel, so a
       stale rate path silently outranked a signed contract and every
       optimizer/reporting consumer priced the household's Smith-Manoeuvre
       spread off a guess -- while ``simulation.py``'s engine, which reads
       tier 2 only, charged the real rate. One run, two rates, no warning.
       The contract loader no longer writes this key at all.
    2. ``property.heloc_rate`` -- the FACT. The household's own declared
       CURRENT HELOC rate (mapped from ``liabilities[kind=heloc].rate``,
       #654's canonical spelling), and the ONLY tier a contract-loaded
       document populates. A signed rate is not an assumption, so nothing
       short of a deliberate scenario decision may displace it.

    #654 fixed only the core ``simulation.py`` pricing path
    (``FamilySimulation``/``CanadaAdapter.build_heloc_path``, which reads
    tier 2 only -- an anchor override does not retroactively reprice the
    engine's own per-year simulation, only the optimizer's screening
    heuristics that consult this resolver). #656/#677 found the identical
    mortgage-rate-aliasing pattern still live in the optimizer's OWN
    ranking/reporting paths (``optimizer.py``, ``optimize.py``,
    ``scenario_discovery.py``) -- silently understating the after-tax
    Smith-Manoeuvre cost by however much the HELOC and mortgage rates
    actually differ, always in the direction that flatters leverage. Every
    one of those consumers must resolve the rate here instead of reading
    ``cfg.get('property', {}).get('mortgage_rate', ...)`` as a stand-in.

    When NEITHER tier is declared:

    - A household with NO readvanceable facility at all
      (``has_readvanceable_facility(cfg)`` is ``False``) gets ``default``
      back unconditionally. The value can never be surfaced -- every
      caller that reaches this branch filters the readvanceable strategy
      back out regardless of what rate was used to screen it (#663) -- so
      no placeholder derivation or warning is needed; callers may pass an
      inert placeholder (e.g. ``0.0``).
    - A household that DOES declare a facility (``property.
      margin_available`` present) but never declared its own HELOC rate is
      a hand-built config that predates the ``heloc_rate`` field -- every
      real contract (``input_contract.py``) declares both together, the
      schema requires it on a ``kind=heloc`` liability -- so this can only
      happen for this test suite's legacy fixtures, never a real
      household. It gets the mortgage rate as a DP#13
      placeholder-derivation basis (the same approximation
      ``countries.canada.rate_model.HELOCPath.get_heloc_rate`` falls back
      to when its own ``fixed_rate`` is ``None``), and the fallback is
      logged -- not silent -- so the approximation is visible in the run's
      output rather than quietly priced off a different credit product.
    """
    assumptions = cfg.get('assumptions', {})
    rate = assumptions.get('heloc_rate')
    if rate is not None:
        return rate
    prop = cfg.get('property', {})
    rate = prop.get('heloc_rate')
    if rate is not None:
        return rate
    if not has_readvanceable_facility(cfg):
        return default
    if 'mortgage_rate' in prop:
        logger.warning(
            "property.heloc_rate was never declared for a household with "
            "a readvanceable facility (property.margin_available present) "
            "-- pricing this HELOC/Smith-Manoeuvre borrowing at the "
            "MORTGAGE rate as a DP#13 placeholder (issues #654/#656/#677). "
            "Set property.heloc_rate (liabilities[kind=heloc].rate in a "
            "real contract) to price this correctly; the HELOC and the "
            "mortgage are different credit products and are not expected "
            "to share a rate."
        )
        return prop['mortgage_rate']
    return default


@dataclass
class YearResult:
    """Snapshot of family finances for one year of simulation."""
    year: int = 0

    # Rates
    mortgage_rate: float = 0.0
    heloc_rate: float = 0.0

    # Income
    primary_income: float = 0.0
    spouse_income: float = 0.0
    total_family_income: float = 0.0
    annual_savings: float = 0.0

    # Contributions this year
    contributions: Dict[str, float] = field(default_factory=dict)

    # Account balances (end of year)
    primary_rrsp: float = 0.0
    spousal_rrsp: float = 0.0
    spouse_rrsp: float = 0.0
    total_rrsp: float = 0.0
    primary_tfsa: float = 0.0
    spouse_tfsa: float = 0.0
    total_tfsa: float = 0.0
    resp_balance: float = 0.0
    non_reg_balance: float = 0.0
    total_assets: float = 0.0

    # RESP wind-down (issue #578): EAP (taxable to the student, not the
    # household) and PSE (contributions returned tax-free to the subscriber)
    # paid out this year, plus the AIP tax cost if the plan collapsed unused.
    resp_eap_paid: float = 0.0
    resp_pse_paid: float = 0.0
    resp_aip_tax: float = 0.0

    # Debt
    mortgage_balance: float = 0.0
    heloc_balance: float = 0.0
    total_debt: float = 0.0

    # Tax
    primary_marginal: float = 0.0
    spouse_marginal: float = 0.0
    bracket_gap: float = 0.0
    rrsp_tax_savings: float = 0.0
    # Issue #546: per-year deduct-later claim slices, each
    # {'year': contribution_year, 'amount': claimed, 'rate': bracket-fill rate}.
    deduction_claims: List[Dict] = field(default_factory=list)
    # Issue #546: cumulative advantage of the staggered deduct-later schedule
    # over deducting the same total amount all in a single (first-claim) year.
    # Defined as (sum of staggered bracket-fill savings across claim years)
    # minus (bracket-fill value of deducting that whole total in the first
    # claim year, where later dollars of the lump sink into low brackets).
    # >= 0 always; > 0 exactly when spreading the deduction keeps slices in
    # higher brackets year over year instead of wasting room on low brackets.
    deduction_advantage_vs_now: float = 0.0
    readvance_interest: float = 0.0
    readvance_tax_savings: float = 0.0
    # Issue #1083: the s.20(1)(c) deduction's statutory (bracket-fill) tax
    # saving on the retired primary's PROLOGUE-taxed slice (rental/loan
    # income), for the share of the deduction the cpp+pension drawdown base
    # could not absorb. Booked as cash by ``apply_solvency`` (the
    # tuition_credit path); 0.0 in every accumulation year and for any
    # household whose deduction fits inside the drawdown base. The flat
    # objective side-credit (``readvance_tax_savings``) stays 0 in retirement
    # -- this is the taxable-income routing, not its return.
    sm_interest_nondrawdown_tax_saving: float = 0.0
    sm_qc_deductible: float = 0.0
    sm_qc_carry_forward: float = 0.0
    sm_deductible_proportion: float = 0.0  # From HELOC tracing

    # Issue #850: the s.20(1)(c) position of the OTHER two borrowings a year-0
    # leveraged lump sum creates -- the mortgage ADVANCE (cash-out) and the
    # DRAWN revolving margin. Both were previously deducted NOWHERE, so
    # advance-vs-line was ranked on the rate gap and interest capitalization
    # alone, with the deductibility asymmetry that motivates the choice (#849)
    # set to zero on both sides.
    #
    # The two *_deductible_balance fields are the instrument that measures that
    # asymmetry directly: the ADVANCE's falls year over year as the mortgage
    # amortizes (a fixed deductible proportion of a shrinking balance -- the
    # erosion #849 names), while the LINE's does not (it is interest-only and
    # capitalizes into the charge). All 0.0 for a household that borrowed no
    # lump sum -- e.g. the golden household (DP#32).
    advance_deductible_balance: float = 0.0
    advance_deductible_interest: float = 0.0
    margin_deductible_balance: float = 0.0
    margin_deductible_interest: float = 0.0
    # The tax actually saved by the two legs above, AFTER the shared QC
    # investment-expense cap. Distinct from readvance_tax_savings (the SM
    # readvance line's own share of that same cap).
    traced_borrowing_tax_savings: float = 0.0

    # Issue #693 (epic #690 bite 2): a declared rental property's income for the
    # year. `net_rental_income` is gross rent minus operating expenses minus the
    # deductible mortgage interest (ITA s.20(1)(c)) -- the ordinary-income figure
    # added to the owner's taxable income and to household after-tax cash (CRA
    # form T776). `rental_interest_deductible` is the s.20(1)(c) mortgage-interest
    # deduction embedded in it, surfaced separately so the deductibility is
    # observable. Both 0.0 for a household that declares no couple-owned rental
    # (the golden path) -- a no-op there (DP#32).
    #
    # Issue #694 (epic #690 bite 3): a rental may also elect Capital Cost
    # Allowance -- non-cash depreciation of the building. `cca_claimed` is the
    # year's CCA (already netted OUT of `net_rental_income` above, since CCA is a
    # T776 deduction line, but NOT out of after-tax cash: it is non-cash);
    # `rental_ucc` is the running per-property undepreciated capital cost, threaded
    # to next year and read by the estate to recapture the CCA at the deemed
    # disposition (ITA s.13(1)). Both inert (0.0 / {}) with no CCA election.
    net_rental_income: float = 0.0
    rental_interest_deductible: float = 0.0
    cca_claimed: float = 0.0
    rental_ucc: Dict[str, float] = field(default_factory=dict)

    # Issue #697 (epic #690 bite 6): a SHORT-TERM rental (Airbnb-style) is
    # ACTIVE business income (ITA s.9), not passive property income. Its net
    # income is included in net_rental_income above (business income is ordinary
    # income too), but surfaced HERE distinctly as `str_business_income` -- the
    # subset of net_rental_income earned by declared STR properties. Its gross
    # revenue is tested against the $30k GST/HST small-supplier threshold (ETA
    # s.148): `gst_hst_registration_required` is True when any STR's gross rent
    # exceeds it. Both inert (0.0 / False) for a household with no STR (the
    # golden path -- a long-term rental never triggers the flag, DP#32).
    str_business_income: float = 0.0
    gst_hst_registration_required: bool = False

    # Issue #702: the year's attribution-rule DETECTION over the declared
    # private loans -- ITA s.74.1 (spousal property transfer), s.74.2 (minor
    # child) and s.74.5(1) (below-market / prescribed-rate-loan escape) -- one
    # dict per loan, computed by simulation._attribution_checks_for from the
    # rule functions in countries.canada.attribution (DP#10). DETECTION ONLY:
    # it changes no tax number (the one attribution flow the engine PRICES --
    # a minor LENDER's interest attributed to the borrower under s.74.2 -- is
    # #929's wiring in private_loan_interest.classify_private_loan_interest).
    # Empty for a household that declares no private loans (the golden path,
    # DP#32: no trigger data -> no entries, zero overhead).
    attribution_summary: List[Dict] = field(default_factory=list)

    # Mortgage details
    mortgage_payment: float = 0.0
    mortgage_interest: float = 0.0
    mortgage_principal: float = 0.0
    # issue #681: sm_readvanced is what the CHARGE allowed to be re-borrowed
    # this year -- NOT simply mortgage_principal (which is what it used to
    # be, when the readvance was unbounded and a household could re-borrow
    # its way to 382% LTV). readvance_room is the room the shared charge
    # left at the moment of the draw; readvance_blocked is principal repaid
    # that could NOT be re-borrowed because the charge was full. A blocked
    # readvance is reported, never silently truncated (DP#32).
    sm_readvanced: float = 0.0
    readvance_room: float = 0.0
    readvance_blocked: float = 0.0
    # issue #681: a revolving facility cannot capitalize interest past the
    # charge (it used to, unboundedly -- a $400k margin draw compounded to
    # $1M+ of revolving debt straight through a $720k charge, and the run
    # stayed green). What the charge had room for is capitalized; the rest is
    # SERVICED IN CASH out of non-registered savings, then out of the SM
    # investment itself; anything the household cannot fund at all is
    # reported here rather than silently absorbed (DP#32).
    heloc_interest_capitalized: float = 0.0
    heloc_interest_serviced: float = 0.0
    heloc_interest_unfunded: float = 0.0
    # Issue #1069: the year's TOTAL margin interest charged (opening drawn
    # balance x rate), surfaced so the split below is CHECKABLE rather than
    # taken on faith: capitalized + serviced must equal it, and serviced must
    # equal funded + unfunded -- the identity
    # 'heloc_interest_fully_accounted' asserts every year in the fold (DP#18).
    heloc_interest_charged: float = 0.0
    # Issue #1034: forced dispositions of the non-reg and SM pots to service
    # HELOC interest realize a taxed capital gain and reduce the cost basis
    # proportionally (matching sm_unwind, which reuses price_sm_unwind).
    # ``heloc_servicing_realized_gain`` is the year's total pre-inclusion gain
    # from both legs; it is wired into apply_amt's realized_gain base (the
    # AMT minimum-tax side) and surfaced here for transparency (DP#32).
    # ``heloc_servicing_tax`` is the tax paid (funded by grossing the sale up).
    # The taxable slice (``heloc_servicing_taxable``) is an internal AMT
    # accumulator on YearWorkingState, not surfaced here. 0.0 in every year no
    # pot is sold to service interest (incl. the golden household) -- inert,
    # byte-identical (DP#32).
    heloc_servicing_realized_gain: float = 0.0
    heloc_servicing_tax: float = 0.0
    # Issue #1069: what the pots actually DELIVERED toward the serviced
    # interest (net of the gross-up tax), accumulated leg by leg. With
    # ``heloc_interest_unfunded`` it closes the serviced-slice identity:
    # heloc_interest_serviced == heloc_servicing_funded +
    # heloc_interest_unfunded, asserted every year by the run-path invariant.
    heloc_servicing_funded: float = 0.0
    # Issue #1031: the Smith-Manoeuvre investment SLEEVE -- the leveraged
    # non-registered portfolio that lives in jurisdiction_state['canada']
    # (NOT the top-level non_reg_balance), tracked separately because it was
    # financed by the readvanceable HELOC. Surfaced here so the estate's deemed
    # disposition (#1031) can read the terminal FMV / cost basis / HELOC debt
    # without reaching into the jurisdiction dict, and so a test/output surface
    # can observe them directly (DP#32). All 0.0 for a household with no SM
    # sleeve (the golden household) -- inert, byte-identical (DP#32).
    sm_investment_balance: float = 0.0      # SM portfolio FMV (separate sleeve)
    sm_investment_cost_basis: float = 0.0   # SM portfolio ACB (DP#19)
    sm_heloc_balance: float = 0.0           # SM readvance line debt financing it
    # Issue #1017: the liquidate-to-target SM unwind -- when the ordinary
    # financial drawdown exhausts every drawable account and a shortfall
    # remains, a slice of the SM sleeve is sold (realizing a capital gain), the
    # proceeds repay the SM HELOC proportionally, the capital-gains tax is paid,
    # and the NET funds the spending target. Surfaced so a test/output surface
    # can observe the unwind directly (DP#32). All 0.0 in every year no unwind
    # fires (no SM sleeve, or liquidate_to_target off, or no shortfall) -- the
    # golden household -> inert, byte-identical (DP#32).
    sm_unwind_proceeds: float = 0.0         # SM portfolio FMV sold this year
    sm_unwind_tax: float = 0.0              # capital-gains tax on the realized gain
    sm_unwind_heloc_repaid: float = 0.0     # SM HELOC principal repaid
    sm_unwind_net_delivered: float = 0.0    # net to the spending target

    # ACB tracking (DP#19: track cost basis from day one)
    non_reg_acb: float = 0.0           # Adjusted cost base for non-reg
    non_reg_unrealized_gains: float = 0.0  # balance - acb
    # Issue #754: the capital gain REALIZED this year by disposing of non-reg
    # assets -- proceeds minus ACB, at 100% (NOT the 50%-included taxable slice,
    # which is already folded into drawdown_taxable). Sums the retirement-drawdown
    # disposition and any forced solvency-waterfall liquidation. 0.0 in every year
    # the non-reg pot is only accumulated (never disposed) -- e.g. every
    # pre-retirement year (DP#32). Surfaced so a year-end AMT assessment (#710,
    # ITA s.127.52(1)(d): 100% inclusion) and other consumers read a real realized
    # base instead of re-deriving it from the bundled taxable total.
    # Issue #956 bite B (sale-core): also folds in the realized gain from a
    # declared mid-horizon property SALE (ws.sale_realized_gain), so the AMT
    # base sees a real realized base from the property disposition too.
    realized_capital_gains: float = 0.0
    # Issue #956 bite B (sale-core): the net proceeds (P_net) invested into
    # non-reg this year from declared property SALES, and the capital-gains
    # tax (T) those sales crystallized (net of the PRE apportionment). The tax
    # is already netted out of P_net (the household receives the after-tax
    # proceeds); surfaced here for transparency (DP#32), NOT added to the
    # year's ordinary income-tax base (the tax is computed once, in T). Both
    # 0.0 in every year no property is sold (the golden household -- DP#32).
    sale_proceeds_invested: float = 0.0
    sale_disposition_tax: float = 0.0
    # Issue #956 bite E (principal-residence disposition): the net proceeds
    # (P_net) invested into non-reg this year from a declared SALE of the
    # PRINCIPAL residence, the PRE-apportioned capital-gains tax (T, already
    # netted out of P_net), the pre-inclusion realized gain (for the AMT
    # base), and the secured debt discharged at the sale (mortgage + HELOC +
    # SM-HELOC -- the debt leg of the net_assets conservation identity). The
    # principal's value is NOT in total_assets (it flows via house_value /
    # charge math), so the conservation identity is on NET_ASSETS
    # (Δnet_assets = V - selling_costs - T), not total_assets. Surfaced for
    # transparency (DP#32); NOT added to the ordinary income-tax base (the
    # tax is computed once, in T). All 0.0 in every year no principal is sold
    # (the golden household -- DP#32).
    principal_sale_proceeds_invested: float = 0.0
    principal_sale_disposition_tax: float = 0.0
    principal_sale_realized_gain: float = 0.0
    principal_sale_discharged_debt: float = 0.0
    # Issue #710: the Alternative Minimum Tax surcharge assessed this year --
    # max(0, minimum amount - regular federal tax after credits), booked on top
    # of the regular tax the fold already priced (the household is charged
    # max(regular, AMT)). 0.0 in every year the fold realizes no capital gain,
    # which is every year AMT cannot bite in this engine (#754) -- so 0.0 for
    # the golden household in all 46 years (DP#32).
    amt_surcharge: float = 0.0
    # Issue #747: the Quebec impôt minimum de remplacement surcharge (a separate
    # provincial minimum tax, TP-776.42), the minimum-tax credit recovered this
    # year against regular tax (ITA s.120.2), and the closing federal credit
    # balance carried forward. All 0.0 for a household that never pays a minimum
    # tax (the golden household, all 46 years -- DP#32).
    qc_imr_surcharge: float = 0.0
    amt_credit_recovered: float = 0.0
    qc_imr_credit_recovered: float = 0.0
    amt_credit_balance: float = 0.0
    # Issue #1082: the net minimum-tax charge assessed this year (new
    # surcharges minus recovered credits, floored at 0) and the slice of it the
    # non-registered pot could not fund -- reported, never silently absorbed
    # (DP#32; before #1082 an unfunded remainder simply vanished, understating
    # tax by up to $380k in the issue's reported case -- fabricated round
    # figures, DP#4/DP#15). Both 0.0 whenever no minimum
    # tax is assessed or the pot fully funds it (the golden household).
    amt_net_charge: float = 0.0
    amt_unfunded: float = 0.0

    # CRI/LIRA and LIF (issue #230)
    lira_balance: float = 0.0          # CRI/LIRA balance (accumulation phase)
    lif_balance: float = 0.0          # LIF balance (decumulation phase)
    lif_withdrawal: float = 0.0       # LIF withdrawal this year (taxable income)

    # Retirement transition (issue #294): government income + drawdown that
    # engage once a member crosses retirement_age within the projection.
    cpp_income: float = 0.0           # Combined CPP/QPP for the family this year
    oas_income: float = 0.0           # Combined OAS (net of clawback) this year
    # Issue #1033: the OAS 15% recovery-tax (clawback) the drawdown + forced
    # RRIF minimum triggered this year -- the drawdown+RRIF slice (the
    # preliminary recovery tax ``member_retirement_income`` booked on the
    # CPP+pension base, before ``sm_interest`` runs, is bundled into
    # ``oas_income`` above and NOT broken out here). Surfaced for TEST /
    # OPTIMIZER observability: the s.20(1)(c) investment-interest deduction
    # (routed through ``drawdown_other_taxable_income`` in
    # ``apply_retirement_drawdown``) REDUCES this, and the deferred
    # income-flowing half will later RAISE it. NOTE: this field is NOT in
    # ``output_plugins.YEAR_COLUMNS`` -- it is read by tests/optimizer, not by
    # the report renderer (NEW-4: the docstring no longer overstates "observable
    # on YearResults" as if reports surface it). 0.0 in every pre-retirement year
    # (no OAS) and for a household below the recovery-tax threshold.
    oas_clawback: float = 0.0
    pension_income: float = 0.0       # Combined defined-benefit pension this year
    # Issue #1020 (S04 Step 1): GIS (Guaranteed Income Supplement) paid to
    # the family this retirement year. The simulation fold now calls the
    # existing ``countries.canada.retirement.gis_benefit`` (DP#9: reused, not
    # re-spelled) from ``apply_retirement_income`` using the PRIOR year's
    # GIS-countable income (CRA's prior-year income test — OAS is excluded
    # from the test base, per the helper's documented ``net_income`` contract).
    # Folded into ``retirement_income`` / ``total_family_income`` / the
    # drawdown ``covered_net`` (GIS is cash that covers spending, so it
    # reduces the discretionary drawdown shortfall). 0.0 in every pre-retirement
    # year and for every GIS-ineligible household (high income -> GIS=0,
    # DP#32). The golden household is GIS-ineligible, so this stays 0.0 across
    # its whole 46-year horizon and the golden invariant is byte-unchanged.
    gis_income: float = 0.0           # GIS paid this year (prior-year income test)
    drawdown_income: float = 0.0      # Registered/non-reg drawdown this year (gross)
    drawdown_taxable: float = 0.0     # Portion of drawdown taxable as ordinary income
    # Issues #363/#579: the requested NET (after-tax) spending target for the
    # year's discretionary drawdown, and what plan_drawdown_net actually
    # delivered net of tax. Equal within tolerance unless account balances ran
    # out (delivered < target). 0.0 in every pre-retirement / non-drawdown year.
    drawdown_net_target: float = 0.0
    drawdown_net_delivered: float = 0.0
    # Issue #707: the NET spending gap the household could NOT fund from
    # drawdown because every drawable account was exhausted -- target minus
    # delivered, only in a year the target was positive AND the post-drawdown
    # balance across every drawable account is ~0. 0.0 in every year the
    # target was met (or no drawdown was requested). Distinct from
    # ``solvency_shortfall`` (#679), which is the cash-flow identity gap
    # against declared ``household_budget.annual_living_costs`` and fires
    # whether or not the drawdown itself fell short. The two can co-exist:
    # a household whose drawdown already ran out may ALSO fail the cash-flow
    # identity. DP#32: a bankrupt year must not be reported as just another
    # number -- this field is what makes the shortfall a directly testable
    # fact rather than something only observable by re-deriving target -
    # delivered from two other fields.
    drawdown_shortfall: float = 0.0
    retirement_income: float = 0.0    # CPP + OAS + pension + drawdown (total)
    employment_income: float = 0.0    # Family employment income (post-retirement stop)
    # Issue #758: True when any member is past retirement_age this year (the
    # retirement drawdown model is active). The #679 solvency rule uses the
    # RETIREMENT spending target in these years (not the working-phase
    # living_costs); #758's runway metric uses this to scope itself to
    # WORKING life -- a retirement-year solvency event (e.g. a mortgage not
    # fully paid off by retirement) is a retirement-plan question (#707's
    # domain), not a shock-induced working-life runway event.
    any_retired: bool = False

    # Net benefit calculation
    net_benefit: float = 0.0

    # ── Solvency (issue #679) ──────────────────────────────────────────
    # The cash-flow identity checked every year by simulation_rules
    # .apply_solvency: after_tax_income + drawdown_net_delivered >=
    # debt_service + living_costs + contributions. All zero/empty/False in
    # every year the household's own living-cost budget was never supplied
    # (household_budget.annual_living_costs absent -- DP#16, the module
    # does not run without its trigger data).
    after_tax_income: float = 0.0       # Employment income net of tax this year
    living_costs: float = 0.0           # This year's declared working-phase budget
    # Issue #761: the spending figure actually CHARGED in the cash-flow
    # identity's `required` term this year. Equals `living_costs` in every
    # year except a working-life income-shock year where the household
    # declared a discretionary split -- then it is the NON-discretionary
    # portion (living_costs * (1 - discretionary_fraction)), the
    # discretionary portion having been compressed to zero. 0.0 in every
    # year the solvency rule did not run (no budget declared). Reported so
    # the identity is transparent about which figure it charged, never a
    # silent substitution (DP#32).
    solvency_spending_outflow: float = 0.0
    # Issue #761: discretionary dollars compressed to zero this year under
    # an income shock (living_costs * discretionary_fraction, only in a
    # working-life shock year where a split was declared). 0.0 otherwise --
    # including when no split was declared, which is the "all rigid"
    # assumption the output names explicitly via model_fidelity. A positive
    # value is the labelled stress assumption (discretionary cuts to zero
    # under a shock), not a silent default.
    solvency_discretionary_compressed: float = 0.0
    # Issue #760: this year's dated living-cost segment outflow actually CHARGED
    # in the solvency identity's spending term (on top of `living_costs`) --
    # each active segment's annual amount prorated by the days of [from, to)
    # falling in this calendar year, MINUS any discretionary segment compressed
    # to zero by an income shock. 0.0 in every year no segment is active (before
    # a segment's `from`, after its `to` -- it ENDS, never carried to the
    # horizon) and in every year no segments were declared. The discretionary
    # segment dollars compressed under a shock are folded into
    # solvency_discretionary_compressed above, so the identity stays transparent.
    expense_segment_outflow: float = 0.0
    debt_service: float = 0.0           # Mortgage payment this year (the identity's debt-service term)
    contributions_total: float = 0.0    # This year's ACTUAL booked contributions (post room-clamping), the identity's contributions term
    solvency_shortfall: float = 0.0     # required - available, before any liquidation (0 if solvent)
    solvency_covered: float = 0.0       # Net dollars the liquidation waterfall actually delivered
    forced_liquidation_tax: float = 0.0        # Total tax paid across every waterfall step this year
    forced_liquidation_realized_loss: float = 0.0  # Sum of negative realized_gain across steps (issue #679: reported honestly, never floored at 0)
    # Per-step detail: [{'source', 'gross_drawn', 'net_proceeds', 'tax', 'realized_gain'}, ...],
    # in the order actually drawn (emergency_reserve -> revolving_credit ->
    # non_reg -> tfsa -> registered). Empty in every solvent year.
    forced_liquidation_events: List[Dict] = field(default_factory=list)

    # ── Emergency reserve (issue #688) ─────────────────────────────────
    # The reserve is a CASH SLEEVE CARVED OUT of the account named in
    # assumptions.emergency_reserve.held_in -- NOT extra money on top of
    # it. It grows at its own declared instrument rate, never the
    # portfolio's (a reserve compounding at the equity return is not a
    # reserve), and the #679 waterfall draws it FIRST.
    emergency_reserve_balance: float = 0.0   # End-of-year reserve (cash sleeve)
    emergency_reserve_target: float = 0.0    # target_months x essential outflows, this year
    # Months of THIS YEAR's essential outflows the reserve actually covers.
    # The number the household needs to hear ("you have 2 months, you said
    # you wanted 12"); 0.0 when no reserve is declared -- a hard, stated
    # zero, never an assumed-away one (DP#32).
    emergency_reserve_months_covered: float = 0.0

    # ── Revolving credit facility (issue #689) ──────────────────────────
    # `liabilities[kind=line_of_credit]` -- a revolving, interest-only
    # facility distinct from the mortgage-paired HELOC (`heloc_balance`
    # above), secured or not (SimulationConfig.credit_facility_secured).
    # 0.0 at year 0 and in every year no shortfall reached it -- an undrawn
    # facility costs nothing (DP#32: this is the correct value, not a
    # fallback). Rises only when the #679 waterfall draws it in a shortfall
    # year, at which point it is real debt, accruing interest at
    # credit_facility_rate every year after, folded into total_debt.
    credit_facility_balance: float = 0.0
    # True when a shortfall was reported (or would have been) while no
    # line_of_credit was declared at all (credit_facility_limit <= 0) -- the
    # household's real resilience may be understated in that year, because a
    # real facility the household holds simply was not stated as input. This
    # is now a genuine, representable absence (fixed by declaring the
    # facility), not a structural gap in the engine (which #689 closed).
    credit_facility_unrepresentable: bool = False

    # Issue #763: closed-end consumer loans (car_loan/student_loan/
    # personal_loan). The total DRAWN balance (folded into total_debt above)
    # and this year's payment / interest -- the payment is the
    # consumer-debt half of the solvency identity's debt-service term
    # (apply_solvency), the interest is its non-deductible (consumption)
    # component. Both 0 for a household with no consumer debt.
    consumer_loan_balance: float = 0.0
    consumer_loan_payment: float = 0.0
    consumer_loan_interest: float = 0.0

    # Issue #759: fixed-term, zero-interest installment obligations. The
    # remaining-payment balance (the sum of monthly payments + final balloon
    # still owed forward -- a REPORTING figure, deliberately NOT folded into
    # total_debt above: an installment plan is a committed payment schedule,
    # not a callable borrowing against the estate) and this year's payment --
    # the installment half of the solvency identity's debt-service term
    # (apply_solvency), at 0% interest so there is no interest component. The
    # payment drops to 0 the year after the final payment date (the plan
    # ENDS -- it is not carried to the horizon the way annual_living_costs
    # is). Both 0 for a household with no installment plan.
    installment_balance: float = 0.0
    installment_payment: float = 0.0

    # Issue #967: mid-horizon mortgages originated by properties'
    # `purchase.financing`. The outstanding balance (the sum of each financed
    # property's end-of-year mortgage balance -- folded into total_debt above)
    # and this year's payment / interest -- the payment is the
    # second-property-mortgage half of the solvency identity's debt-service
    # term (apply_solvency), the interest is the portion DEDUCTIBLE when the
    # property is a rental (ITA s.20(1)(c), claimed by the rental fold) and
    # NON-deductible for a recreational/personal property. All 0 for a
    # household with no financed property (the golden path) -- byte-identical
    # (DP#32).
    second_property_mortgage_balance: float = 0.0
    second_property_mortgage_payment: float = 0.0
    second_property_mortgage_interest: float = 0.0
    # Issue #967: the principal ORIGINATED this year (non-zero only in a
    # financed property's purchase year). An INFLOW to the solvency identity
    # that funds the purchase (only the down payment leaves the portfolio),
    # surfaced for transparency + money-conservation checks. 0 in every year
    # but a purchase year, and for a household with no financing (DP#32).
    second_property_mortgage_originated: float = 0.0

    # True only when the waterfall exhausted every source and the household
    # is STILL short this year -- a hard ruin, not a modeled cost (DP#32:
    # this must be checked explicitly by any caller reporting a terminal
    # net_benefit; net_benefit itself is NOT zeroed here, since a truthful
    # report needs both "what the ledger says" and "whether that ledger was
    # ever actually achievable" -- see tests/test_issue_679_solvency.py).
    ruined: bool = False
    # Issue #784: per-member unused tuition-tax-credit carry-forward at the
    # END of this year (the remainder carried to the next year). 0.0 for a
    # household that declares no tuition. Surfaced for transparency so a
    # reader can see the credit was carried, not discarded.
    primary_tuition_carryforward: float = 0.0
    spouse_tuition_carryforward: float = 0.0

    # Epic #841 bite 4: end-of-year snapshot of each child's OWN accounts (the
    # bite-2 child_accounts list -- one dict per child with rrsp/tfsa/fhsa/
    # non_reg balances and their room/acb). Threaded here as REPORTING data so
    # the family objective (max_family_after_tax_networth) can value every
    # member's wealth, NOT summed into total_assets(): a household with no
    # child-savers (the golden household -- its children are RESP-only) carries
    # an empty-or-all-zero list here, so total_assets() and every existing
    # objective are bit-identical (DP#32). Copied from the terminal
    # jurisdiction_state['canada']['child_accounts'] each year.
    child_accounts: List[Dict] = field(default_factory=list)

    # Issue #899 (part a): end-of-year snapshot of each ADDITIONAL accumulating
    # adult's OWN RRSP/TFSA (the adults beyond the primary couple -- slots >= 2
    # of the per-adult stores). One dict per extra adult, keyed by the same
    # stable entity id the storage layer uses. Threaded here as REPORTING data,
    # mirroring child_accounts: NOT summed into the two-slot total_assets (so a
    # two-adult household carries an empty list here and every existing objective
    # + the golden invariant are bit-identical, DP#32), but read back by the
    # family objective so an extra adult's wealth is not silently dropped.
    extra_adult_accounts: List[Dict] = field(default_factory=list)


@dataclass
class ScenarioOverlay:
    """DP#18: A scenario is a base config plus a set of overlay deltas.

    Per DP#18, overlays modify a base configuration - they don't replace it.
    Fields default to None (meaning "no change from base") rather than 0.0
    (which would silently zero out base income).

    DP#24: Supports round-trip serialization via from_dict()/to_dict()/to_json().
    """""
    label: str
    cash_out: float = 0.0
    resp_cash_out: float = 0.0
    primary_income: Optional[float] = None
    spouse_income: Optional[float] = None
    mortgage_rate: float = 0.05  # DP#13: round-number placeholder
    use_readvanceable: bool = False
    deduct_later: bool = False
    investment_return: Optional[float] = None
    ltv: Optional[float] = None
    # DP#14/DP#5: Sensitivity overlay fields
    inflation: Optional[float] = None      # Override inflation rate (affects real return)
    salary_growth: Optional[float] = None   # Override salary growth rate
    # Issue #303: retirement-age dimension. None means "no change from base".
    retirement_age: Optional[int] = None
    # Issue #655: the amortization a cash_out > 0 refinance is repaid over.
    # None means "use the base config's property.refinance_amortization_years";
    # if that is also absent, apply_overlay refuses (DP#32) rather than
    # silently inheriting property.amortization_years.
    refinance_amortization_years: Optional[int] = None
    # Issue #688: the emergency-reserve target, in months of essential
    # outflows. THE sweep dimension the reserve question needs (0/3/6/12/24):
    # sweeping it shows the household the real trade -- expected terminal
    # wealth against the probability and cost of a forced sale (#679's
    # waterfall). None means "no change from base" (DP#18), which is NOT the
    # same as 0 ("hold no reserve") -- a swept 0 is a real, declared decision.
    emergency_reserve_months: Optional[float] = None
    # Issue #735: what fraction of margin_available is drawn and invested at
    # year 0 (0.0-1.0). 0.0 (the default) is the correct value for a scenario
    # that never asks about this dimension, NOT `None` -- a declared facility
    # simply is not drawn unless something draws it (DP#32), so there is no
    # "no change from base" ambiguity to preserve the way there is for
    # emergency_reserve_months (0 reserve-months is a real declared choice
    # distinct from "never asked"; 0 draw-fraction is the ONLY value that
    # makes sense as an unstated default). Sweep it with several
    # ScenarioOverlay instances (e.g. [0.0, 0.25, 0.5, 1.0]) to see the
    # trade-off the Smith-Manoeuvre question actually is.
    draw_fraction: float = 0.0
    # Issues #711/#712: the retirement income-splitting elections the optimizer
    # sweeps (DP#22/#30). ``cpp_share`` is the fraction (0..1) of the way to a
    # fully equalized CPP/QPP split between two retired spouses; ``pension_split_pct``
    # is the fraction (0..0.5) of the higher-bracket spouse's eligible pension
    # allocated to the lower-bracket spouse on a T1032 election. None means "no
    # change from base" (DP#18) — distinct from an explicit 0 ("elect not to
    # split"), both real declarable values; the engine default when neither the
    # overlay nor the base retirement block sets one is no split, preserving
    # current behaviour (DP#13/#32).
    cpp_share: Optional[float] = None
    pension_split_pct: Optional[float] = None
    # Issue #936: the deposit product this scenario TAKES (the single declared
    # product dict), or None for the implicit "leave it" baseline. When set,
    # apply_overlay carves the product's fund_amount out of its funding_source
    # (money-conserving) and lands it on cfg['deposit_product'] -- the key
    # SimulationConfig.from_dict reads and the engine acts on (DP#18). None
    # means "this scenario takes no product" -- distinct from a $0 product, and
    # the value every non-deposit sweep dimension carries, so the golden
    # invariant is untouched (DP#32). Each declared product + this None baseline
    # is a ranked take/leave candidate the optimizer scores head-to-head (#936
    # capability #4).
    deposit_product: Optional[Dict] = None

    def __init__(self, label: str, cash_out: float = 0.0, resp_cash_out: float = 0.0,
                 primary_income: Optional[float] = None, spouse_income: Optional[float] = None,
                 mortgage_rate: float = 0.05,
                 use_readvanceable: bool = False, deduct_later: bool = False,
                 investment_return: Optional[float] = None,
                 ltv: Optional[float] = None,
                 inflation: Optional[float] = None,
                 salary_growth: Optional[float] = None,
                 retirement_age: Optional[int] = None,
                 refinance_amortization_years: Optional[int] = None,
                 emergency_reserve_months: Optional[float] = None,
                 draw_fraction: float = 0.0,
                 cpp_share: Optional[float] = None,
                 pension_split_pct: Optional[float] = None,
                 deposit_product: Optional[Dict] = None):
        """Create a ScenarioOverlay."""
        self.label = label
        self.cash_out = cash_out
        self.resp_cash_out = resp_cash_out
        self.primary_income = primary_income
        self.spouse_income = spouse_income
        self.mortgage_rate = mortgage_rate
        self.use_readvanceable = use_readvanceable
        self.deduct_later = deduct_later
        self.investment_return = investment_return
        self.ltv = ltv
        self.inflation = inflation
        self.salary_growth = salary_growth
        self.retirement_age = retirement_age
        self.refinance_amortization_years = refinance_amortization_years
        self.emergency_reserve_months = emergency_reserve_months
        self.draw_fraction = draw_fraction
        self.cpp_share = cpp_share
        self.pension_split_pct = pension_split_pct
        self.deposit_product = deposit_product



    def to_dict(self) -> Dict:
        """DP#24: Serialize overlay to a plain dict.

        Only includes non-default values for optional fields to keep
        the output compact. The label is always included.

        Returns:
            Dict suitable for JSON serialization or from_dict() round-trip.
        """
        result = {'label': self.label}

        # Always include numeric fields (they have meaningful defaults)
        if self.cash_out != 0.0:
            result['cash_out'] = self.cash_out
        if self.resp_cash_out != 0.0:
            result['resp_cash_out'] = self.resp_cash_out
        if self.primary_income is not None:
            result['primary_income'] = self.primary_income
        if self.spouse_income is not None:
            result['spouse_income'] = self.spouse_income

        # mortgage_rate is always included (has a meaningful default)
        result['mortgage_rate'] = self.mortgage_rate

        # Boolean flags
        if self.use_readvanceable:
            result['use_readvanceable'] = True
        if self.deduct_later:
            result['deduct_later'] = True

        # Optional fields
        if self.investment_return is not None:
            result['investment_return'] = self.investment_return
        if self.ltv is not None:
            result['ltv'] = self.ltv

        # DP#14/DP#5: Sensitivity overlay fields
        if self.inflation is not None:
            result['inflation'] = self.inflation
        if self.salary_growth is not None:
            result['salary_growth'] = self.salary_growth

        # Issue #303: retirement-age dimension
        if self.retirement_age is not None:
            result['retirement_age'] = self.retirement_age

        # Issue #655: refinance amortization
        if self.refinance_amortization_years is not None:
            result['refinance_amortization_years'] = self.refinance_amortization_years

        # Issue #688: emergency-reserve target sweep dimension
        if self.emergency_reserve_months is not None:
            result['emergency_reserve_months'] = self.emergency_reserve_months

        # Issue #735: draw-fraction sweep dimension
        if self.draw_fraction != 0.0:
            result['draw_fraction'] = self.draw_fraction

        # Issues #711/#712: retirement income-splitting sweep dimensions
        if self.cpp_share is not None:
            result['cpp_share'] = self.cpp_share
        if self.pension_split_pct is not None:
            result['pension_split_pct'] = self.pension_split_pct

        # Issue #936: the taken deposit product (None = "leave it" baseline)
        if self.deposit_product is not None:
            result['deposit_product'] = self.deposit_product

        return result

    @classmethod
    def from_dict(cls, data: Dict) -> 'ScenarioOverlay':
        """DP#24: Deserialize overlay from a plain dict.

        Round-trips with to_dict():
            overlay = ScenarioOverlay(label="test", cash_out=100000)
            assert overlay == ScenarioOverlay.from_dict(overlay.to_dict())

        Args:
            data: Dict with overlay fields (as produced by to_dict()).

        Returns:
            New ScenarioOverlay instance.
        """
        return cls(
            label=data['label'],
            cash_out=data.get('cash_out', 0.0),
            resp_cash_out=data.get('resp_cash_out', 0.0),
            primary_income=data.get("primary_income"),
            spouse_income=data.get("spouse_income"),
            mortgage_rate=data.get('mortgage_rate', 0.05),
            use_readvanceable=data.get('use_readvanceable', False),
            deduct_later=data.get('deduct_later', False),
            investment_return=data.get('investment_return'),
            ltv=data.get('ltv'),
            inflation=data.get('inflation'),
            salary_growth=data.get('salary_growth'),
            retirement_age=data.get('retirement_age'),
            refinance_amortization_years=data.get('refinance_amortization_years'),
            emergency_reserve_months=data.get('emergency_reserve_months'),
            draw_fraction=data.get('draw_fraction', 0.0),
            cpp_share=data.get('cpp_share'),
            pension_split_pct=data.get('pension_split_pct'),
            deposit_product=data.get('deposit_product'),
        )

    def to_json(self, path: str = None, indent: int = 2) -> str:
        """DP#24: Serialize overlay to a JSON string."""
        return _dict_to_json(self.to_dict(), path=path, indent=indent)

    @classmethod
    def from_overlay_diff(cls, diff: Dict, base: 'SimulationConfig') -> 'ScenarioOverlay':
        """DP#24: Create a ScenarioOverlay from an overlay_diff result.

        Maps dot-path keys in the diff to ScenarioOverlay fields.
        Not all diff entries map to overlay fields; unmapped entries are
        derived effects (e.g. margin_available changes from cash_out).
        """
        overlays = diff.get('overlays', {})
        kwargs = {'label': 'from-diff'}

        # Direct mappings: diff keys → overlay field names
        _key_map = {
            'property.mortgage_rate': 'mortgage_rate',
            'assumptions.investment_return': 'investment_return',
            'assumptions.inflation': 'inflation',
            'assumptions.salary_growth': 'salary_growth',
            'property.ltv_max': 'ltv',
        }
        for diff_key, field_name in _key_map.items():
            if diff_key in overlays:
                kwargs[field_name] = overlays[diff_key]['to']

        # income: extract from family.members diffs
        for key, change in overlays.items():
            if key.startswith('family.members') and 'gross_income' in key:
                val = change['to']
                if '.0.' in key or key.endswith('.0.gross_income'):
                    kwargs['primary_income'] = val
                elif '.1.' in key or key.endswith('.1.gross_income'):
                    kwargs['spouse_income'] = val

        # cash_out: derived from mortgage_balance change
        if 'property.mortgage_balance' in overlays:
            kwargs['cash_out'] = overlays['property.mortgage_balance']['to'] - base.mortgage_balance

        # resp_cash_out: derived from resp_current_balance going to 0
        if 'accounts.resp_current_balance' in overlays:
            new_resp = overlays['accounts.resp_current_balance']['to']
            if new_resp == 0 and base.resp_current_balance > 0:
                kwargs['resp_cash_out'] = base.resp_current_balance

        return cls(**kwargs)

    @classmethod
    def extract(cls, base_cfg: dict, derived_cfg: dict) -> 'ScenarioOverlay':
        """DP#24: Extract a ScenarioOverlay from a base and derived config dict pair.

        Compares the two dicts to determine what overlay was applied.
        """
        kwargs = {'label': 'extracted'}

        base_prop = base_cfg.get('property', {})
        deriv_prop = derived_cfg.get('property', {})
        base_acct = base_cfg.get('accounts', {})
        deriv_acct = derived_cfg.get('accounts', {})
        base_fam = base_cfg.get('family', {})
        deriv_fam = derived_cfg.get('family', {})
        base_asmp = base_cfg.get('assumptions', {})
        deriv_asmp = derived_cfg.get('assumptions', {})

        # Income
        base_members = base_fam.get('members', [])
        deriv_members = deriv_fam.get('members', [])
        for m in deriv_members:
            if m.get('role') == 'primary':
                base_income = next(
                    (bm['gross_income'] for bm in base_members if bm.get('role') == 'primary'),
                    m['gross_income']
                )
                if m['gross_income'] != base_income:
                    kwargs['primary_income'] = m['gross_income']
            elif m.get('role') == 'spouse':
                base_income = next(
                    (bm['gross_income'] for bm in base_members if bm.get('role') == 'spouse'),
                    m['gross_income']
                )
                if m['gross_income'] != base_income:
                    kwargs['spouse_income'] = m['gross_income']

        # Mortgage rate
        if deriv_prop.get('mortgage_rate') != base_prop.get('mortgage_rate'):
            kwargs['mortgage_rate'] = deriv_prop['mortgage_rate']

        # Cash out
        cash_out = deriv_prop.get('mortgage_balance', 0) - base_prop.get('mortgage_balance', 0)
        if cash_out != 0:
            kwargs['cash_out'] = cash_out

        # LTV
        if deriv_prop.get('ltv_max') != base_prop.get('ltv_max'):
            kwargs['ltv'] = deriv_prop['ltv_max']

        # RESP cash out
        if deriv_acct.get('resp_current_balance', -1) == 0 and base_acct.get('resp_current_balance', 0) > 0:
            kwargs['resp_cash_out'] = base_acct['resp_current_balance']

        # Investment return — DP#21/#591 (#990): the engine reads return_model,
        # not the deprecated assumptions.investment_return scalar. apply_overlay
        # writes the swept rate via set_return_rate into return_model, so the
        # scalar is stale/absent on the derived config and the old comparison
        # here read a dead key — a return-rate overlay round-tripped through
        # extract as "no change" (DP#24 broken). Resolve the representative
        # year-0 rate from the source of truth on both sides; a fixed overlay
        # always lands a fixed return_model (set_return_rate), so the derived
        # resolved rate is the swept value and round-trips back into the overlay.
        base_rate = resolve_return_rate(base_cfg)
        deriv_rate = resolve_return_rate(derived_cfg)
        if deriv_rate != base_rate:
            kwargs['investment_return'] = deriv_rate

        # DP#14/DP#5: Sensitivity overlay fields
        if deriv_asmp.get('inflation') != base_asmp.get('inflation'):
            kwargs['inflation'] = deriv_asmp.get('inflation')
        if deriv_asmp.get('salary_growth') != base_asmp.get('salary_growth'):
            kwargs['salary_growth'] = deriv_asmp.get('salary_growth')

        return cls(**kwargs)


@dataclass
class SimulationConfig:
    """Configuration for a simulation run.

    Can be created from input.json or manually constructed.
    """
    projection_years: int = 10
    # DP#1: the horizon as a date-computed rule ("plan to age N"). When set it
    # derives projection_years via projection_span(); kept so to_dict() can
    # re-emit it for round-trip (DP#24).
    horizon_age: Optional[int] = None
    investment_return: float = 0.07  # DP#21 DEPRECATED: Use return_model_data instead. Remove in v2.0.
    salary_growth: float = 0.02
    inflation: float = 0.025  # DP#13: round-number placeholder; read from config
    tfsa_growth: float = 0.02
    savings_rate: float = 0.0  # DP#13: personal data - set from config (0 = not set)
    capital_gains_inclusion: float = 0.50
    resp_eap_tax_rate: float = 0.15
    resp_eap_taxable_portion: float = 0.60

    # RESP wind-down (issue #578): the study window is computed from each
    # beneficiary's birth_year + resp_study_start_age, for
    # resp_study_duration_years. DP#28: date-computed, configurable -- not a
    # magic age hardcoded into the simulation fold. resp_used_for_education
    # is a family decision (DP#30), not a government rule: when False, the
    # plan collapses (grant repayment + AIP) once the beneficiary reaches
    # study_start_age instead of paying out EAPs.
    resp_study_start_age: int = 18
    resp_study_duration_years: int = 4
    resp_used_for_education: bool = True
    # Real contributions/CESG/QESI/earnings breakdown of resp_current_balance,
    # when known (DP#13: real data always wins over the resp_rules default
    # 50/10/5/35 split used when this is absent).
    resp_composition: Dict = field(default_factory=dict)

    # Property
    house_value: float = 0  # DP#13: set from input.json, not hardcoded
    # Issue #963 (epic #956 bite F): the principal residence's REAL annual
    # appreciation rate (dimensionless, e.g. 0.03 for 3%/yr; negative allowed),
    # mirroring Bite A's `appreciation_rate` on a non-principal property.
    # None = a static value (byte-identical to today, DP#32): the consumers'
    # absence-test returns `house_value` and never reads this field, so the
    # golden fixture (whose legacy `property` dict never carries this key)
    # round-trips unchanged. When declared, the principal's value compounds at
    # `house_value * (1 + rate) ** (cal_year - start_year)` everywhere the
    # home's value is read: the LTV/charge math (annual), the estate's
    # deemed-disposition FMV (terminal), and Bite E's principal sale gross
    # (sale year). A sweepable numeric leaf (sensitivity.sweeps).
    appreciation_rate: Optional[float] = None
    mortgage_balance: float = 0
    mortgage_rate: float = 0.05
    # Issue #1075 (data-model half): the sum of the balances of every
    # kind=mortgage tranche whose `deductible` flag is set -- their interest
    # is deductible under ITA s.20(1)(c) (borrowed for an income-producing
    # non-registered investment) -- and, alongside it, those tranches' EXACT
    # annual interest (each balance * its OWN rate). Mapped by
    # input_contract.py off property.deductible_mortgage_balance /
    # property.deductible_mortgage_interest, emitted only when > 0 so a
    # household with no deductible tranche (the golden fixture and every
    # pre-#1075 contract) round-trips byte-identical (DP#32: absence of the
    # flag is "not deductible", never a fabricated zero key). Surfaced HERE
    # so the s.20(1)(c) interest pricing (issue #850) can price the declared
    # tranches' TRUE interest without re-deriving the tracing from the
    # contract. The interest is NOT deductible_mortgage_balance * rate: the
    # rate is the balance-weighted average of ALL tranches, which equals the
    # deductible tranches' own-rate sum only when every tranche shares one
    # rate (DP#32: never price deductible interest off a blended rate).
    deductible_mortgage_balance: float = 0.0
    deductible_mortgage_interest: float = 0.0
    ltv_max: float = 0.80
    amortization_years: int = 25
    margin_available: float = 200000
    # issue #663/DP#32: whether this contract declares a readvanceable HELOC
    # facility AT ALL -- a first-class state, distinct from margin_available
    # being 0. Set in from_dict() from the presence of property.margin_available
    # in the source cfg (has_readvanceable_facility()); NOT derived from
    # margin_available > 0, which cannot tell "no facility" apart from "a
    # facility with zero undrawn room". Consumers that decide whether to rank
    # the readvanceable-mortgage strategy, or quote a HELOC rate/after-tax
    # cost, must check this instead of margin_available's truthiness.
    #
    # Defaults True (matching margin_available's own legacy default of
    # 200000, DP#13) so the ~4,000 tests that build SimulationConfig
    # directly -- not through from_dict()/a contract -- keep their existing
    # "assume a HELOC exists" behaviour unchanged. from_dict() is the one
    # place that computes the real answer from contract-mapped data.
    #
    # NOTE: to_dict() does not conditionally re-emit margin_available from
    # this flag -- it always writes the field, same as every other release
    # of this dataclass, so existing "mutate a field, then export/reload"
    # callers (see tests/test_new_features.py's round-trip suite) keep
    # working. The dict-level crash/misreporting this issue actually fixes
    # lives entirely upstream of here, in apply_overlay() and the raw-dict
    # cfg passed around optimize.py/simulate.py -- those never fabricate
    # margin_available, so a contract with no facility stays that way
    # through every overlay. has_heloc exists for callers that hold a
    # SimulationConfig built straight from a real contract (from_dict) and
    # need the true answer without re-deriving it from margin_available.
    has_heloc: bool = True
    # issue #654: the household's OWN declared HELOC rate --
    # liabilities[kind=heloc].rate, mapped by from_dict() off
    # property.heloc_rate. A HELOC is a different credit product from the
    # mortgage it may sit alongside (a cheap legacy fixed mortgage plus a
    # prime-linked revolving HELOC is the ordinary case, not an edge case)
    # and the two rates must never be conflated (DP#32). None means "no
    # HELOC rate was ever declared" -- distinct from a declared 0. The
    # engine (FamilySimulation) honours a declared value outright; it only
    # falls back to a mortgage-derived approximation (DP#13 placeholder,
    # not a fallback that overrides supplied input) when this is None.
    heloc_rate: Optional[float] = None
    # issue #654: liabilities[kind=heloc].rate_type ("fixed"/"variable"),
    # mapped alongside heloc_rate for round-trip completeness (DP#24) and
    # future rate-path modelling. Not yet consumed by the engine beyond
    # that: a declared heloc_rate is a single current-year scalar either
    # way (see FamilySimulation.__init__'s heloc_path construction) --
    # there is no year-over-year HELOC rate PATH in this schema/engine yet
    # for "variable" to switch on (that would be assumptions.rate_paths.
    # heloc's job, which is unwired end-to-end; see #654's PR notes).
    heloc_rate_type: Optional[str] = None
    # issue #257: refinance cash-out (mortgage increase whose proceeds are
    # invested). Recorded as debt once (via mortgage_balance); the proceeds are
    # added to the invested lump sum. margin_available is NOT inflated by this.
    cash_out: float = 0.0

    # issue #1039: the OPENING DRAWN position of the mortgage-paired HELOC
    # (liabilities[kind=heloc].balance.amount), mapped by from_dict() off
    # property.heloc_opening_balance. 0.0 is #577's documented undrawn state.
    # input_contract.py writes the key only when the contract declares a drawn
    # balance WITH its deductibility, so absence here means "no opening draw"
    # -- never a coerced zero (DP#32). Seeded into SimState.heloc_balance by
    # SimState.initial() and carried by the fold; margin_available has already
    # been reduced by the same draw upstream (undrawn room = limit - drawn).
    heloc_opening_balance: float = 0.0
    # issue #1039: the declared deductible proportion
    # (deductibility.investment_portion) of that OPENING drawn balance. The
    # original borrowing's purpose is a historical fact that predates the
    # snapshot, so it is carried in as a declared ratio -- not re-derived from
    # a simulation decision (#577 governs draws the engine makes). Consumed by
    # SimState.initial() to seed canada.margin_tracing; only meaningful with
    # heloc_opening_balance > 0.
    heloc_opening_investment_portion: float = 0.0

    # issue #735: what FRACTION of margin_available is drawn and invested at
    # year 0. 0.0 is the DP#32-correct default -- a declared facility that is
    # simply left UNDRAWN, not a fabricated draw the household never made.
    # Before this fix, every caller sized the year-0 lump sum as
    # `margin_available + cash_out` unconditionally (simulation_state.
    # margin_draw_for_lump_sum), so a declared facility was ALWAYS drawn in
    # full and invested -- charging its cost (interest on a fully drawn
    # balance) while denying its benefit (standby liquidity in a #679
    # shortfall, which can only ever find room there if the room was never
    # spent). This is a SEARCH DIMENSION (DP#22/DP#31: the optimizer ranks,
    # it doesn't choose), not a SimulationConfig field: a household does not
    # "declare" what fraction of a facility it will draw at time zero of a
    # hypothetical projection, so there is no fact for a config field to
    # hold. Each caller that sizes a year-0 lump sum computes
    # `margin_available * draw_fraction + cash_out` with its OWN draw
    # fraction, explicit at the call site -- ``GridOptimizer.optimize()``'s
    # ``draw_fraction_options`` parameter (default ``[0.0]``, sweepable),
    # ``optimize.run_optimization()``'s parameter of the same name, and
    # ``ScenarioOverlay.draw_fraction`` (``simulate.py``'s path) -- rather
    # than a config field that would sit unread by two of those three call
    # sites (DP#9/DP#18: a value nothing reads is not a feature).

    # issue #689: a revolving, interest-only credit facility DISTINCT from
    # the mortgage-paired HELOC above -- ``liabilities[kind=line_of_credit]``.
    # Kept as its own facility, deliberately not merged into
    # margin_available/heloc_balance: a HELOC is carved out of the same
    # registered charge as the mortgage and its balance's tax deductibility
    # is TRACED by use (ITA s.20(1)(c), countries/canada/debt.py) -- treating
    # a household's investment-loan room as if it were emergency liquidity
    # would poison that tracing and model a different product than the one
    # asked about (see simulation_rules.apply_solvency's docstring). This
    # facility exists for exactly one purpose today: it is the #679
    # liquidation waterfall's second rung (emergency reserve -> THIS ->
    # non-registered -> TFSA -> registered) -- available capital in a
    # shortfall year that costs nothing while undrawn (DP#32: 0.0 is the
    # correct default, not a fallback masking a missing input; nothing has
    # been borrowed until a real shortfall draws it).
    credit_facility_limit: float = 0.0
    # None means "no line_of_credit liability was ever declared" -- distinct
    # from a declared 0 (DP#32), mirroring heloc_rate's own None-vs-0
    # convention above.
    credit_facility_rate: Optional[float] = None
    credit_facility_rate_type: Optional[str] = None
    # issue #689: whether this facility is registered against the property
    # (liabilities[kind=line_of_credit].collateral is non-null and matches
    # the principal residence) -- decides whether its DRAWN balance counts
    # toward the OSFI B-20 charge limit (#664/#681) at all. An unsecured
    # facility is genuinely ADDITIONAL capacity outside that charge, and
    # survives a sale of the property; a secured one does not (#689).
    credit_facility_secured: bool = False

    # issue #664: the LTV ceiling of the ONE registered charge against the
    # property -- mortgage balance + drawn/limit revolving HELOC must fit
    # inside charge_limit(house_value, charge_ltv_limit). DP#13 fallback:
    # OSFI B-20's legal maximum for an uninsured combined loan plan (see
    # OSFI_B20_CHARGE_LTV_MAX docstring above); a contract that declares its
    # own charge terms should set this explicitly.
    charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX
    # issue #664: OSFI B-20's revolving-only ceiling -- independent of
    # charge_ltv_limit, the readvanceable/revolving segment alone may not
    # exceed this fraction of house_value.
    heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX
    # issue #655: the amortization a cash-out refinance is repaid over. NOT
    # like amortization_years above -- deliberately has NO numeric default
    # (DP#32). A refinance is a new loan; apply_ltv_overlay/apply_overlay
    # refuse to book a cash-out when this is None (and no explicit override
    # is supplied) rather than silently inheriting the incumbent mortgage's
    # remaining amortization_years. Sourced from
    # decisions.mortgage.refinance_options[].amortization_years when a
    # contract declares one.
    refinance_amortization_years: Optional[int] = None

    # Issue #792: the dollar amount of the refinance advance the household
    # DECLARES it will route into the DEDUCTIBLE non-reg account first, before
    # filling registered room. Sourced from the first declared
    # decisions.mortgage.refinance_options[].advance_split.deductible_non_reg.
    # None means "no declared split" -- the engine keeps today's internal
    # optimization (fill registered first, non-reg gets the remainder); this is
    # the default-off state that preserves current behaviour (DP#13/DP#32).
    # A declared 0 is a real choice (route nothing to deductible non-reg) and
    # is carried as 0.0, distinct from absence -- both are honoured by
    # StrategyEngine.fill_room.
    refinance_advance_deductible_non_reg: Optional[float] = None

    # Family
    family_members: List[Dict] = field(default_factory=list)
    children: List[Dict] = field(default_factory=list)
    private_loans: List[Dict] = field(default_factory=list)  # issue #813: private loans (lender income + borrower s.20(1)(c) deduction)
    gifts: List[Dict] = field(default_factory=list)  # epic #841 bite 3: parent->child gifts funding a child's registered room
    first_home_purchases: List[Dict] = field(default_factory=list)  # issue #704: a member's first-home purchase -> FHSA qualifying withdrawal + HBP

    # Accounts
    rrsp_annual_percent: float = 0.18
    rrsp_annual_max: float = 0  # DP#13: set from TaxDataProvider; 2026 value: 33810
    tfsa_annual_room_per_person: float = 7000

    # DP#45: deduct-later bracket target - income threshold below which
    # RRSP deductions are not claimed (they stay undeducted until income
    # rises above this bracket). Default: federal 20.5%/26% boundary for 2026.
    deduct_later_bracket_target: float = 0  # DP#13: set from tax brackets; 2026 federal 26% boundary: 117045

    # RESP (from resp_rules module)
    resp_current_balance: float = 0.0

    # DP#8/DP#10 (#241): jurisdiction is config data, not a hardcoded literal.
    # Core modules read these instead of defaulting to 'canada'/'quebec' in code.
    # province accepts either the long form ('quebec') or the postal code ('qc');
    # TaxDataProvider resolves both via PROVINCES aliases. Default preserves the
    # historical Quebec behaviour for configs that omit a tax.province.
    country: str = 'canada'
    province: str = 'quebec'

    # DP#20: year-versioned simulation
    start_year: int = 2026
    frozen_brackets: bool = False  # If True, use start_year brackets for all years (sensitivity isolation per DP#5)
    time_step: str = 'yearly'     # 'yearly' or 'monthly' (monthly enables intra-year events)

    # DP#18: CashFlow events - irregular income, planned expenses, lifecycle events
    cash_flows: List[Dict] = field(default_factory=list)

    # DP#27: Portfolio composition - per-account investment income types
    portfolio_data: Dict = field(default_factory=dict)

    # DP#16: Readvanceable mortgage trigger - derived from mortgage config data,
    # not a standalone boolean flag. When heloc_readvance=True in input.json
    # AND margin_available > 0, the simulation auto-enables the readvanceable
    # mortgage investment-loan strategy.
    # The use_readvanceable parameter in FamilySimulation overrides this when
    # explicitly set (for optimizer comparisons); None means auto-detect.
    heloc_readvance: bool = False

    # Issue #956 bite E (principal-residence disposition): a declared
    # mid-horizon SALE of the principal residence, mapped by
    # `contract_principal._map_principal_sale` onto `cfg['property']['principal_sale']`.
    # None / absent = the principal is held to the horizon -> a strict no-op
    # (DP#32): the golden invariant is unchanged by construction (the golden
    # household builds SimulationConfig.from_dict straight from a legacy dict
    # that never carries `principal_sale`, so this stays None there). When
    # present, the `principal_disposition` rule fires in the sale year: the
    # home + its mortgage + any HELOC/SM secured against it leave the balance
    # sheet from the sale year, and the net proceeds (gross value less the
    # discharged debt, selling costs, and the PRE-apportioned disposition tax)
    # are invested into non-reg POST-GROWTH. The shape is the SAME Bite B
    # carries on a non-principal `sale` (year/selling_costs/owner_roles/
    # designated_principal_residence_years/value_share/acb_share) -- one
    # consistent contract the disposition rules read.
    principal_sale: Optional[Dict] = None

    # DP#9 (epic #603 Track C Phase 2): heloc_data / rental_data /
    # rate_scenario_data / employer_benefits_data / life_events_data used to
    # live here. Each was stashed straight from the raw config dict
    # (`cfg.get('heloc', {})` etc.) and read by NOTHING outside this loader
    # and tests -- HELOCConfig/RentalProperty/LifeEvent/RateScenarioConfig
    # (the from_dict() parsers that would have consumed them) had zero
    # production callers (#593). Deleted along with their schema leaves
    # rather than kept as unwired features (DP#9: a feature that has never
    # run is not a feature, it's a liability). The HELOC rate the engine
    # actually uses is derived from property.mortgage_rate
    # (simulation.py's build_heloc_path); rate_type is hardcoded 'variable'.

    # DP#28: Retirement extended data
    retirement_data: Dict = field(default_factory=dict)

    # epic #603 Track C Phase 2c (#600): the DECLARED estate elections
    # (spousal rollover, TFSA successor holder vs beneficiary, the real
    # non-registered ownership split, the principal-residence designation,
    # life insurance, per-person mortality). Every one of these used to be a
    # silent assumption inside countries/canada/estate.py, and every one
    # resolved in the favourable direction. Consumed by
    # objective.plan_from_config -> estate.EstatePlan.
    estate_data: Dict = field(default_factory=dict)

    # DP#2/DP#13: Non-reg investment yield rate - configurable, not hardcoded.
    # Default 2% is a conservative fallback; actual yield depends on portfolio
    # composition (dividend ETFs ~3-5%, growth stocks ~0.5%, bonds ~4%).
    # Used by Quebec interest deduction limit calculation (DP#10/DP#27).
    non_reg_yield_rate: float = 0.02

    # DP#21: Return model configuration
    return_model_data: Dict = field(default_factory=dict)

    # DP#16/issue #293: CRI/LIRA (locked-in pension) data. Lives at the top
    # level of input.json (separate from the RRSP balance held inside the
    # family member), so it must be captured here and threaded into
    # SimState.initial. Without this the $52,837 locked-in account is silently
    # dropped and never grows or appears in total_assets.
    lira_data: Dict = field(default_factory=dict)

    # Issue #679: the household's own MEASURED working-phase living costs (a
    # fact -- 12 months of bank/credit statements), separate from the
    # assumptions.savings_rate BELIEF. None means "not supplied"
    # (DP#32/DP#16): the cash-flow solvency invariant
    # (simulation_rules.apply_solvency) does not run without it, exactly as
    # the LTV explorer needs house_value+mortgage_balance+margin_available
    # together. A household legitimately never states living_costs=0 --
    # nobody spends nothing to live -- so treating None/absent as "off" is
    # not the DP#32 zero-as-fallback trap; it is the trigger-data pattern.
    living_costs: Optional[float] = None

    # Issue #761: the DISCRETIONARY fraction (0..1) of `living_costs` -- the
    # share a household cuts FIRST under an income shock (restaurants,
    # vacations, entertainment). None means "no split declared" (DP#16):
    # the solvency/runway identity treats the whole scalar as rigid and the
    # output says so explicitly (model_fidelity.runway_treats_all_spend_as_rigid
    # fires), never inventing a fraction (DP#32). 0.0 and 1.0 are real,
    # declarable values ("all rigid" / "all discretionary"), distinct from
    # None. Under a dated income shock the identity compresses this portion
    # to zero -- a labelled stress assumption, see apply_solvency.
    discretionary_fraction: Optional[float] = None

    # Issue #760: dated, finite-term living-cost segments layered ON TOP OF the
    # perpetual `living_costs` scalar -- a private-school tuition that ENDS when
    # a child ages out, childcare, a term expense that stops on a date. Each is
    # {description, amount (annual), from, to (nullable = perpetual),
    # non_discretionary}; simulation_rules.apply_solvency prorates each active
    # segment by the days of [from, to) falling in the calendar year (the
    # outflow-side analog of #674's dated income windows) and folds it into the
    # solvency identity's spending outflow, a non-null `to` STOPPING the expense
    # after that date (never carried to the horizon the way `living_costs` is).
    # Empty by default -- a household with no dated segments, never a fabricated
    # entry (DP#24/DP#32). Only present when the contract declared some.
    expense_segments: List[Dict] = field(default_factory=list)

    # Issue #688: the emergency-reserve POLICY -- how many months of
    # essential outflows to hold, WHERE, and held as WHAT.
    #
    # `emergency_reserve_target_months` is None when the household declared
    # no reserve block at all, and 0.0 when it declared one with a zero
    # target. Both hold a $0 reserve, but they are NOT the same statement --
    # the first is an absence, the second a decision -- and the ruin report
    # says which (DP#32). This is why the field is Optional rather than a
    # plain 0.0 default.
    #
    # `emergency_reserve_rate` is the return the reserve's declared
    # instrument actually earns. The reserve grows at THIS rate, never at
    # return_model's (a reserve compounding at 7% is not a reserve). It is
    # a required field of the schema block rather than inferred from
    # `instrument`, because "cash means 2%" is exactly the opinion-in-code
    # DP#2 forbids.
    #
    # `emergency_reserve_held_in` is the account kind the cash sleeve is
    # carved out of ('tfsa'/'non_reg'/'rrsp'/...), resolved by
    # input_contract.py from the declared account id. WHERE is most of the
    # answer: cash inside a TFSA grows tax-free, withdraws with no tax and
    # no penalty, and its room is restored the following January; the same
    # dollars in a non-registered account are taxed on their yield, and in
    # an RRSP are effectively unavailable. None = held outside every
    # declared account (an ordinary chequing balance the contract does not
    # otherwise model), in which case the sleeve is NOT carved out of any
    # account balance.
    emergency_reserve_target_months: Optional[float] = None
    emergency_reserve_rate: Optional[float] = None
    emergency_reserve_held_in: Optional[str] = None
    emergency_reserve_instrument: Optional[str] = None

    # Issue #763: closed-end consumer liabilities (car_loan, student_loan,
    # personal_loan) -- amortizing, unsecured, non-revolving debt the
    # household services alongside the mortgage. Each entry is a dict of the
    # contract's own static facts: {id, kind, balance (opening), rate,
    # payment_monthly, amortization_years}. The engine amortizes this list
    # year by year (simulation_rules.apply_consumer_loans), folds the
    # annual payment into the solvency identity's debt-service term
    # (apply_solvency) and the reserve/runway sizing (#758), and carries the
    # declining balance on SimState / YearResult.total_debt.
    #
    # A car/student/personal loan is NOT the mortgage (DP#7): it is a
    # separate unsecured amortizing liability, never aliased onto
    # mortgage_balance. Interest is NOT deductible (consumption debt); the
    # #656 default-to-deductible guard lives at the contract boundary
    # (input_contract refuses investment_portion > 0 loudly).
    consumer_loans: List[Dict] = field(default_factory=list)

    # Issue #692 (epic #690 bite 1): the couple's NON-principal real properties
    # -- a cottage, a rental -- each a dict of static facts {id, kind,
    # net_equity} produced by contract_property._map_owned_properties. Their net
    # equity (value - mortgage secured against that property, at the couple's
    # ownership share) is added to the annual balance sheet: SimState.initial
    # seeds SimState.property_equities from this list and total_assets() sums
    # them in. Before this seam only the single principal residence's value was
    # carried (prop_cfg -> house_value), so a declared cottage/rental was absent
    # from every annual metric and surfaced only at the terminal estate (#692).
    # Absence-safe: an empty list is a household with no such property (the
    # golden path), and the run is byte-identical -- the golden invariant must
    # not move (DP#32). This bite carries a STATIC equity figure; rental income
    # (#693), CCA (#694), the PRE allocation (#695), a purchase event (#696) and
    # STR (#697) are later bites that give the property dynamics.
    properties: List[Dict] = field(default_factory=list)

    # Issue #759: fixed-term, zero-interest installment obligations -- a
    # medical/dental/education payment plan (up-front lump already paid, then
    # N equal monthly payments + optional final balloon, 0% interest, finite
    # term). Each entry is a dict of the contract's own static facts:
    # {id, owner, description, start_date, monthly_amount, number_of_payments,
    # final_payment, rate, non_discretionary}. The engine services this list
    # year by year (simulation_rules.apply_installments), folds the annual
    # payment into the solvency identity's debt-service term (apply_solvency)
    # and the reserve/runway sizing (#758), and carries the declining
    # remaining-payment balance on SimState / YearResult -- but NOT in
    # total_debt: an installment plan is a committed payment schedule for
    # services already received, not a callable borrowing against the estate
    # (contrast SimulationConfig.consumer_loans, which IS real debt). The
    # payment STOPS the year after the final payment date -- it is never
    # carried to the horizon the way household_budget.annual_living_costs is.
    # Absence-safe: an empty list is a household with no such plan, and the
    # run is unchanged (the golden invariant must not move, DP#32).
    installments: List[Dict] = field(default_factory=list)

    # Issue #768: private-company equity grants / stock options the household
    # holds. A RECORD, not an asset: no simulation rule reads this list, so
    # every declared grant contributes $0 to all solvency / runway /
    # decumulation metrics by construction -- it is never counted as a liquid
    # resource. The output surfaces each grant as 'recorded, valued $0' so
    # the household knows it was not silently dropped (DP#32: an
    # absence-of-record is not a labelled $0). Absence-safe: an empty list is
    # a household with no such grants, and the run is unchanged (the golden
    # invariant must not move). Each entry is the contract's own static facts:
    # {id, owner, grantor, grant_date, vesting, strike (nullable), liquidity,
    # shares, fully_diluted_pct, notes}.
    equity_grants: List[Dict] = field(default_factory=list)

    # Issue #936: deposit products -- a plain HISA, a term/GIC, a promotional
    # teaser, expressed by ONE generic mechanism (different rate_schedule/cap
    # field values, not different concepts).
    #
    # `deposit_products` is the DECLARED list (the QUESTIONS the household is
    # deciding between). Like `strategies`/`objective`, it is read by the
    # OPTIMIZER layer (scenario_discovery._discover_deposit_products ->
    # simulate.enumerate_overlays), which ranks each product + the implicit
    # "leave it" baseline take-vs-leave -- the simulator does not read it
    # (DP#22: the optimizer ranks, it doesn't choose).
    #
    # `deposit_product` is the SINGLE product a given SCENARIO takes -- apply_overlay
    # writes exactly one declared product here (or leaves it None for the "leave
    # it" baseline). This IS the engine-facing lever: SimState.initial carves
    # `fund_amount` out of `funding_source` (money-conserving, #936 capability
    # #5) and seeds the deposit balance, and simulation_rules
    # .apply_deposit_product_growth grows it at the rate its `rate_schedule`
    # prescribes for the elapsed time since funding, on the portion up to any
    # `rate_eligible_cap` (#936 capabilities #2/#3), taxing the yield as ordinary
    # interest -- 100% taxable at the marginal rate each year (#936 capability
    # #1), not a deferred capital return.
    #
    # Both absence-safe: a household that declares no product gets today's
    # behaviour byte-for-byte (the golden invariant must not move, DP#32: an
    # absent product is not a $0 product). Each entry is the contract's own static
    # facts: {id, label, account_kind, fund_amount, funding_source,
    # rate_schedule:[{rate, duration_days|duration_years?}, ...] (final step
    # open-ended), rate_eligible_cap (optional), tax_character}. Sequence-of-
    # returns / staggered-deployment scoring is the companion issue #937.
    deposit_products: List[Dict] = field(default_factory=list)
    deposit_product: Optional[Dict] = None

    # Issue #1036: `capitalize_interest` is the HELOC's declared interest-
    # handling mode, mapped from liabilities[kind=heloc].capitalize_interest by
    # input_contract. True (the default when the key is absent, e.g. every
    # internal-config test built directly) = capitalize the drawn-margin
    # interest up to the charge, servicing the rest in cash (the pre-#1036
    # behaviour, byte-identical). False = service ALL the drawn-margin interest
    # in cash (a retiree paying HELOC interest in cash is no longer modelled as
    # capitalizing it). Read by simulation_rules.apply_margin_heloc_interest.
    # The internal-config default (absent key) is True so every test that
    # builds the internal dict directly stays byte-identical (DP#32: absence is
    # the fallback, never a coercion).
    #
    # NOTE: `capitalize_interest` is a FACILITY-level fact wired only to the
    # drawn-MARGIN leg (apply_margin_heloc_interest / new_heloc_balance). The
    # SM readvance leg (new_sm_heloc) is untouched -- its interest is priced
    # and deducted by apply_sm_interest, never capitalized into the balance
    # (the readvance grows the line by principal paydown, not by capitalized
    # interest). Wiring it there too is defensible but out of scope here.
    capitalize_interest: bool = True

    # Issue #1040: a declared decisions.borrow_to_invest[] option with
    # hold_draw=true opts its draw OUT of the RRSP-refund HELOC paydown sweep
    # (simulation_rules.apply_rrsp_refund_heloc_paydown): the drawn balance is
    # NOT reduced by the year's RRSP refund -- the refund stays in the
    # household's cash and flows to the usual allocation instead -- while the
    # interest is still priced, deducted, and serviced/capitalized per
    # capitalize_interest. Mapped from property.borrow_to_invest_hold_draw
    # (set per exploration cell by optimize.run_borrow_to_invest_exploration
    # for options that declare hold_draw). The internal-config default (absent
    # key, e.g. every test that builds the internal dict directly, and the
    # golden fixture) is False -- the pre-#1040 debt-sweep behaviour,
    # byte-identical (DP#32: absence is the fallback, never a coercion).
    hold_borrow_to_invest_draw: bool = False

    # Issue #823: per-account expected_return / locked_until overrides,
    # pot-keyed (rrsp/tfsa/non_reg/lira/lif/fhsa). Both default to empty --
    # a household that declares neither gets today's global-rate, fully-
    # liquid behaviour, which is what keeps the golden invariant unchanged
    # (DP#32: an absent override is not a zero override).
    #   return_overrides[kind] = {'override_balance': float,
    #                             'weighted_rate_sum': float}  # sum(bal*rate)
    #   locked[kind] = [{'balance': float, 'unlock_age': int,
    #                    'owner_birth_year': int}, ...]
    account_return_overrides: Dict = field(default_factory=dict)
    account_locked: Dict = field(default_factory=dict)

    # Issue #691: per-account MER fees, pot-keyed. Defaults to empty -- a
    # household that declares no `mer` grows at the fee-free global rate
    # (today's behaviour; keeps the golden invariant unchanged, DP#32: an
    # absent fee is not a zero fee). The growth rule subtracts the balance-
    # weighted fee from the pot's gross rate (net = gross - weighted_mer_sum /
    # pot_total).
    #   mer_drag[kind] = {'mer_balance': float,
    #                     'weighted_mer_sum': float}  # sum(bal*mer)
    account_mer_drag: Dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: str) -> 'SimulationConfig':
        """Load configuration from an on-disk input contract document.

        Epic #603 Track C Phase 2b: this is the SOLE loading boundary --
        ``path`` must be a document conforming to the input contract
        (``schema/input_schema.json`` + ``schema/countries/canada/
        input_schema.json``, composed and validated by
        ``contract_schema.validate_contract``). ``input_contract.load_and_map``
        does the load + validate + map-to-internal-shape in one mandatory
        step (DP#32: not a bypassable adapter a caller could skip) and hands
        the result to the UNCHANGED ``from_dict`` below. There is no more
        "merge a country overlay of legacy defaults into whatever the user
        typed" step -- the contract requires every field explicit; a
        document that omits a required key is a validation error, not a
        silent default (DP#32).
        """
        import input_contract
        cfg = input_contract.load_and_map(path)
        return cls.from_dict(cfg)

    @classmethod
    def from_dict(cls, cfg: Dict) -> 'SimulationConfig':
        """Build a SimulationConfig from its internal dict shape.

        NOT the input-contract wire format (see ``from_json``) -- this is
        the shape ``to_dict()``/``apply_overlay()``/``ScenarioOverlay`` round
        -trip through internally for scenario generation (DP#18/DP#24), and
        what ~4,169 tests construct directly. ``input_contract.py`` is the
        only place that builds this shape FROM a contract document; nothing
        else authors it as an on-disk file.
        """
        _validate_internal_shape(cfg)
        accounts = cfg.get('accounts', {})
        assumptions = cfg.get('assumptions', {})
        horizon_age = assumptions.get('horizon_age')

        return_model_data = _materialize_return_model_data(cfg)
        savings = cfg.get('savings', {})
        prop = cfg.get('property', {})
        family = cfg.get('family', {})

        # Issue #294: per-member retirement_age (default 65). Normalize here so
        # the field is explicit, round-trips via to_dict()'s family.members, and
        # is read uniformly downstream (simulation.py / retirement_transition).
        members = [dict(m) for m in family.get('members', [])]
        for m in members:
            m.setdefault('retirement_age', 65)
            # Issue #699: every member carries a stable entity id. The contract
            # loader (contract_people._map_member) already sets the schema
            # person_id; a config authored directly (tests, ScenarioOverlay)
            # falls back to its role label, which is a stable identity in the
            # two-adult world. member_by_id / the #643 rewrite key off this.
            m.setdefault('id', m.get('role'))

        # DP#8/DP#10 (#241): jurisdiction comes from config data (tax section),
        # not a hardcoded literal in core logic. Falls back to the historical
        # Quebec/Canada default when the config omits it.
        tax = cfg.get('tax', {}) if isinstance(cfg.get('tax', {}), dict) else {}

        start_year = assumptions.get('start_year', 2026)
        return cls(
            projection_years=projection_span(
                horizon_age=horizon_age,
                start_year=start_year,
                members=members,
                projection_years=assumptions.get('projection_years', 10),
            ),
            horizon_age=horizon_age,
            investment_return=assumptions.get('investment_return', 0.07),
            salary_growth=assumptions.get('salary_growth', 0.02),
            inflation=assumptions.get('inflation', 0.025),
            tfsa_growth=assumptions.get('tfsa_growth', 0.02),
            capital_gains_inclusion=assumptions.get('capital_gains_inclusion', 0.50),
            resp_eap_tax_rate=assumptions.get('resp_eap_tax_rate', 0.15),
            resp_eap_taxable_portion=assumptions.get('resp_eap_taxable_portion', 0.60),
            savings_rate=savings.get('rate', 0.0),  # DP#13: personal data, not a default
            house_value=prop.get('house_value', 0),
            # Issue #963 (epic #956 bite F): absence-safe -- .get(key) with no
            # default returns None on a genuinely absent key, never coerces it
            # (DP#32). The golden household's legacy `property` dict never
            # carries this key -> None -> the consumers return static
            # `house_value` and never read this field -> byte-identical.
            appreciation_rate=prop.get('appreciation_rate'),
            mortgage_balance=prop.get('mortgage_balance', 0),
            mortgage_rate=prop.get('mortgage_rate', 0.05),
            # Issue #1075: absence-safe -- .get(key, 0.0) with a real 0
            # default: a household with no deductible tranche keeps 0.0
            # (DP#32), and a declared deductible balance / interest reaches
            # the engine for the #850 pricing to consume. The keys are only
            # ever written by input_contract when > 0, so a legacy dict
            # never carries them.
            deductible_mortgage_balance=prop.get('deductible_mortgage_balance', 0.0),
            deductible_mortgage_interest=prop.get('deductible_mortgage_interest', 0.0),
            ltv_max=prop.get('ltv_max', 0.80),
            amortization_years=prop.get('amortization_years', 13),
            margin_available=prop.get('margin_available', 0),
            # issue #1039: absence-safe -- input_contract writes these keys
            # only when an opening drawn balance is declared, so a legacy
            # dict never carries them and 0.0 is the documented undrawn state
            # (#577), never a coerced zero (DP#32).
            heloc_opening_balance=prop.get('heloc_opening_balance', 0.0),
            heloc_opening_investment_portion=prop.get(
                'heloc_opening_investment_portion', 0.0),
            has_heloc=has_readvanceable_facility(cfg),
            # issue #654: absence-safe -- .get(key) with no default returns
            # None on a genuinely absent key, never coerces it (DP#32).
            heloc_rate=prop.get('heloc_rate'),
            heloc_rate_type=prop.get('heloc_rate_type'),
            cash_out=prop.get('cash_out', 0.0),
            # issue #689: absence-safe, same convention as heloc_rate above.
            credit_facility_limit=prop.get('credit_facility_limit', 0.0),
            credit_facility_rate=prop.get('credit_facility_rate'),
            credit_facility_rate_type=prop.get('credit_facility_rate_type'),
            credit_facility_secured=prop.get('credit_facility_secured', False),
            charge_ltv_limit=prop.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX),
            heloc_ltv_limit=prop.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX),
            refinance_amortization_years=prop.get('refinance_amortization_years'),
            refinance_advance_deductible_non_reg=prop.get('refinance_advance_deductible_non_reg'),
            heloc_readvance=prop.get('heloc_readvance', False),
            # Issue #956 bite E: the principal residence's declared sale
            # (absence-safe -- .get with no default returns None on a
            # genuinely absent key, never coerces it; the golden household's
            # legacy dict never carries this key -> None -> no-op, DP#32).
            principal_sale=prop.get('principal_sale'),
            family_members=members,
            children=family.get('children', []),
            private_loans=family.get('private_loans', []),  # issue #813
            gifts=family.get('gifts', []),  # epic #841 bite 3
            first_home_purchases=family.get('first_home_purchases', []),  # issue #704
            rrsp_annual_percent=accounts.get('rrsp_annual_percent', 0.18),
            rrsp_annual_max=accounts.get('rrsp_annual_max', 0),  # DP#13: set from TaxDataProvider
            tfsa_annual_room_per_person=accounts.get('tfsa_annual_room_per_person', 7000),
            resp_current_balance=accounts.get('resp_current_balance', 0),
            resp_study_start_age=accounts.get('resp_study_start_age', 18),
            resp_study_duration_years=accounts.get('resp_study_duration_years', 4),
            resp_used_for_education=accounts.get('resp_used_for_education', True),
            resp_composition=accounts.get('resp_composition', {}),
            deduct_later_bracket_target=assumptions.get('deduct_later_bracket_target', accounts.get('deduct_later_bracket_target', 0)),  # DP#13: set from tax brackets
            country=tax.get('country', cfg.get('country', 'canada')),
            province=tax.get('province', 'quebec'),
            start_year=assumptions.get('start_year', 2026),
            frozen_brackets=assumptions.get('frozen_brackets', False),
            time_step=assumptions.get('time_step', 'yearly'),
            cash_flows=cfg.get('cash_flows', []),
            portfolio_data=cfg.get('portfolio', {}),
            retirement_data=cfg.get('retirement', {}),
            estate_data=_estate_block(cfg),
            non_reg_yield_rate=assumptions.get('non_reg_yield_rate', 0.02),
            return_model_data=return_model_data,
            # issue #293: top-level CRI/LIRA block (separate from RRSP balance).
            lira_data=cfg.get('lira', {}) or {},
            # issue #679: absence-safe -- .get(key) with no default returns
            # None on a genuinely absent key, never coerces it (DP#32).
            living_costs=cfg.get('household_budget', {}).get('living_costs'),
            # Issue #761: absence-safe -- .get with no default returns None on
            # a genuinely absent key, never coerces it (DP#32). 0.0 is a real
            # declarable "all rigid" value that travels through unchanged.
            discretionary_fraction=cfg.get('household_budget', {}).get('discretionary_fraction'),
            # Issue #760: dated, finite-term living-cost segments. Absence-safe
            # -- .get with no default returns [] (a household with no dated
            # segments), never a fabricated entry; the list is only present
            # when the contract declared some (DP#24/DP#32).
            expense_segments=list(cfg.get('household_budget', {}).get('expense_segments', [])),
            # issue #688: the reserve POLICY. Every one of these is None when
            # the household declared no assumptions.emergency_reserve block --
            # which means a $0 reserve, STATED as an absence, not defaulted
            # into existence (DP#32). `target_months: 0` inside a declared
            # block is a different thing: a deliberate "I hold no reserve."
            emergency_reserve_target_months=_reserve_cfg(cfg).get('target_months'),
            emergency_reserve_rate=_reserve_cfg(cfg).get('rate'),
            emergency_reserve_held_in=_reserve_cfg(cfg).get('held_in'),
            emergency_reserve_instrument=_reserve_cfg(cfg).get('instrument'),
            # issue #763: closed-end consumer loans (car_loan/student_loan/
            # personal_loan). Absence-safe -- .get with no default returns []
            # (a household with no consumer debt), never a fabricated entry,
            # and the list is only present when the contract declared some.
            consumer_loans=list(cfg.get('consumer_loans', [])),
            # issue #692: the couple's non-principal properties. Absence-safe --
            # .get with no default returns [] (a household with only a principal
            # residence, or none), never a fabricated entry; the list is only
            # present when the contract declared a couple-owned non-principal
            # property (DP#24/DP#32).
            properties=list(cfg.get('properties', [])),
            # issue #759: fixed-term installment obligations. Absence-safe --
            # .get with no default returns [] (a household with no payment
            # plan), never a fabricated entry; the list is only present when
            # the contract declared some (DP#24/DP#32).
            installments=list(cfg.get('installments', [])),
            # Issue #768: record-only equity grants. .get with no default
            # returns [] (a household with no such grants), never a fabricated
            # entry; the list is only present when the contract declared some
            # (DP#24/DP#32). No rule reads it -- $0 for solvency by
            # construction.
            equity_grants=list(cfg.get('equity_grants', [])),
            # Issue #936: the DECLARED deposit products (optimizer-swept,
            # read by scenario_discovery/simulate) and the SINGLE taken product
            # apply_overlay wrote onto this scenario's config (engine-facing).
            # Absence-safe -- .get with no default returns []/None (a household
            # with no such product, the golden path), never a fabricated entry
            # (DP#24/DP#32).
            deposit_products=list(cfg.get('deposit_products', [])),
            deposit_product=cfg.get('deposit_product'),
            # Issue #1036: capitalize_interest defaults True when absent
            # (property.capitalize_interest key absent) so every internal-
            # config test built directly stays byte-identical to the pre-#1036
            # capitalization path (DP#32: absence is the fallback, never a
            # coercion of a supplied value). The raw cfg['borrow_to_invest_
            # options'] key is read directly by optimize.run_borrow_to_invest_
            # exploration (the optimizer, not the simulator, DP#22); it is NOT
            # lifted onto a SimulationConfig field (a dead surface -- D7).
            capitalize_interest=prop.get('capitalize_interest', True),
            # Issue #1040: hold_draw defaults False when absent (the pre-#1040
            # RRSP-refund paydown sweep) so every internal-config test built
            # directly stays byte-identical (DP#32: absence is the fallback,
            # never a coercion of a supplied value).
            hold_borrow_to_invest_draw=prop.get('borrow_to_invest_hold_draw', False),
            account_return_overrides=accounts.get('return_overrides', {}) if isinstance(accounts, dict) else {},
            account_locked=accounts.get('locked', {}) if isinstance(accounts, dict) else {},
            account_mer_drag=accounts.get('mer_drag', {}) if isinstance(accounts, dict) else {},
        )

    def member_by_role(self, role: str, default=None):
        """Issue #699: resolve one of this config's members by role label.

        Delegates to the module-level :func:`find_member_by_role` seam so there
        is a single definition of the role->member resolution. ``default`` is
        returned when no member has that role (explicit, not ``or DEFAULT`` --
        DP#32).
        """
        return find_member_by_role(self.family_members, role, default)

    def member_by_id(self, member_id: str, default=None):
        """Issue #699: resolve one of this config's members by stable entity id.

        This is the lookup the multi-generation rewrite (#643) keys off -- an
        id survives a household that has more than two adults, where a role
        string cannot. ``default`` is returned when no member has that id.
        """
        return next((m for m in self.family_members
                     if m.get('id') == member_id), default)

    def adults(self) -> List[Dict]:
        """Issue #699: this config's adult members, primary then spouse.

        The seam every "iterate the taxable adults" site routes through
        (accounts, tax, estate) so Steps 2-8 generalize it to N adults in one
        place instead of ~20 inline role lookups.
        """
        return adult_members(self.family_members)

    @property
    def is_readvanceable(self) -> bool:
        """DP#16: Auto-detect whether the readvanceable mortgage strategy applies.

        Derived from mortgage config data, not a standalone flag.
        Returns True when all conditions hold:
        1. has_heloc (issue #663: a readvanceable facility exists on this
           contract at all -- distinct from margin_available being 0)
        2. heloc_readvance=True (the mortgage product is readvanceable)
        3. margin_available > 0 (there is available HELOC room to readvance into)

        The optimizer can override this by passing use_readvanceable=True/False
        explicitly; None means auto-detect from this property.
        """
        return self.has_heloc and self.heloc_readvance and self.margin_available > 0

    @property
    def should_deduct_later(self) -> bool:
        """DP#16: Auto-detect whether RRSP deductions should be deferred (deduct_later).

        Derived from bracket target config data, not a standalone flag.
        Returns True when deduct_later_bracket_target > 0, meaning the user
        has configured a specific tax bracket target for RRSP deduction timing.
        When deduct_later_bracket_target is 0 (not set), the deduction-later
        module is not activated.

        The optimizer can override this by passing deduct_later=True/False
        explicitly; None means auto-detect from this property.
        """
        return self.deduct_later_bracket_target > 0

    def to_dict(self) -> Dict:
        """Export configuration as a dict matching the input.json schema."""
        return {
            'assumptions': {
                # DP#24: re-emit the horizon rule (when set) alongside the span it
                # derived, so a saved config reloads to the same projection.
                **({'horizon_age': self.horizon_age} if self.horizon_age else {}),
                'projection_years': self.projection_years,
                'investment_return': self.investment_return,
                'salary_growth': self.salary_growth,
                'inflation': self.inflation,
                'tfsa_growth': self.tfsa_growth,
                'capital_gains_inclusion': self.capital_gains_inclusion,
                'resp_eap_taxable_portion': self.resp_eap_taxable_portion,
                'resp_eap_tax_rate': self.resp_eap_tax_rate,
                'start_year': self.start_year,
                'non_reg_yield_rate': self.non_reg_yield_rate,
                'frozen_brackets': self.frozen_brackets,
                'time_step': self.time_step,
                # Issue #688: only re-emitted when the household actually
                # declared a reserve policy. None round-trips to "absent"
                # (no reserve, stated), never to a fabricated block (DP#32) --
                # and re-emitting it is what makes the target sweepable
                # through apply_overlay, which reads it back off here.
                **({'emergency_reserve': {
                    'target_months': self.emergency_reserve_target_months,
                    'rate': self.emergency_reserve_rate,
                    'held_in': self.emergency_reserve_held_in,
                    'instrument': self.emergency_reserve_instrument,
                }} if self.emergency_reserve_target_months is not None else {}),
            },
            'cash_flows': self.cash_flows,
            'savings': {
                'rate': self.savings_rate,
            },
            'property': {
                'house_value': self.house_value,
                # Issue #963 (epic #956 bite F): only re-emit when declared --
                # None means "no appreciation, static value" (the golden path),
                # not a value to round-trip as an explicit null that a naive
                # from_dict() re-read would treat as "declared but empty" (DP#32).
                **({'appreciation_rate': self.appreciation_rate}
                   if self.appreciation_rate is not None else {}),
                'mortgage_balance': self.mortgage_balance,
                'mortgage_rate': self.mortgage_rate,
                # Issue #1075 (DP#24/DP#32): only re-emit when declared --
                # 0.0 round-trips to 'absent' (no deductible tranche), the
                # same absence-safe convention property.cash_out uses, so a
                # load->modify->save cycle never fabricates a deductible
                # balance for a household that has none.
                **({'deductible_mortgage_balance': self.deductible_mortgage_balance}
                   if self.deductible_mortgage_balance else {}),
                # Issue #1075 (DP#24/DP#32): same absence-safe convention as
                # deductible_mortgage_balance -- the exact deductible
                # interest (sum of each flagged tranche's balance * its own
                # rate) round-trips only when a deductible tranche exists.
                **({'deductible_mortgage_interest': self.deductible_mortgage_interest}
                   if self.deductible_mortgage_interest else {}),
                'ltv_max': self.ltv_max,
                'amortization_years': self.amortization_years,
                'margin_available': self.margin_available,
                # Issue #730 (DP#24/DP#18): re-emit a booked refinance
                # cash_out so a load->modify->save cycle does not silently
                # drop the invested-capital source it recorded. Emitted only
                # when non-zero -- 0.0 round-trips to 'absent' (no refinance
                # booked), the same absence-safe convention ScenarioOverlay
                # .to_dict() uses for its own cash_out (#257). Before this,
                # from_dict() ingested property.cash_out but to_dict() never
                # re-emitted it, so any saved config lost the cash-out leg of
                # its refinance on the next load.
                **({'cash_out': self.cash_out} if self.cash_out else {}),
                # Issue #1039 (DP#24): re-emit a declared opening drawn HELOC
                # position so a load->modify->save cycle does not silently
                # drop it. Emitted only when non-zero -- 0.0 round-trips to
                # 'absent' (undrawn, #577), the same absence-safe convention
                # cash_out uses above.
                **({'heloc_opening_balance': self.heloc_opening_balance}
                   if self.heloc_opening_balance else {}),
                **({'heloc_opening_investment_portion':
                    self.heloc_opening_investment_portion}
                   if self.heloc_opening_investment_portion else {}),
                'heloc_readvance': self.heloc_readvance,
                'charge_ltv_limit': self.charge_ltv_limit,
                'heloc_ltv_limit': self.heloc_ltv_limit,
                # DP#24: only re-emit when declared, same pattern as
                # horizon_age above -- None must round-trip to "absent", not
                # a literal null a naive from_dict() re-read would treat as
                # "declared but empty" (DP#32).
                **({'refinance_amortization_years': self.refinance_amortization_years}
                   if self.refinance_amortization_years is not None else {}),
                # Issue #792 (DP#24): only re-emit when declared -- None means
                # "no declared split" (today's internal optimization), not a
                # value to round-trip as an explicit null (DP#32). A declared
                # 0 IS re-emitted (it is a real choice, distinct from absence).
                **({'refinance_advance_deductible_non_reg':
                        self.refinance_advance_deductible_non_reg}
                   if self.refinance_advance_deductible_non_reg is not None else {}),
                # issue #654: only re-emitted when actually declared --
                # None means "never declared" (DP#32), not a value to
                # round-trip as an explicit null.
                **({'heloc_rate': self.heloc_rate} if self.heloc_rate is not None else {}),
                **({'heloc_rate_type': self.heloc_rate_type} if self.heloc_rate_type is not None else {}),
                # Issue #1036 (DP#24): only re-emit capitalize_interest when
                # it is NOT the default (True) -- True round-trips to 'absent'
                # (the pre-#1036 capitalization path, byte-identical), False is
                # a real declared 'service in cash' that must survive a
                # load->modify->save cycle (DP#32).
                **({'capitalize_interest': self.capitalize_interest}
                   if self.capitalize_interest is not True else {}),
                # Issue #1040 (DP#24): only re-emit when declared True --
                # False round-trips to 'absent' (the pre-#1040 paydown sweep,
                # byte-identical), True is a real declared 'hold the draw
                # flat' that must survive a load->modify->save cycle (DP#32).
                **({'borrow_to_invest_hold_draw': self.hold_borrow_to_invest_draw}
                   if self.hold_borrow_to_invest_draw else {}),
                # issue #689: only re-emitted when actually declared -- None
                # means "never declared" (DP#32), same convention as
                # heloc_rate above.
                **({'credit_facility_limit': self.credit_facility_limit,
                    'credit_facility_secured': self.credit_facility_secured}
                   if self.credit_facility_limit > 0 else {}),
                **({'credit_facility_rate': self.credit_facility_rate}
                   if self.credit_facility_rate is not None else {}),
                **({'credit_facility_rate_type': self.credit_facility_rate_type}
                   if self.credit_facility_rate_type is not None else {}),
                # Issue #956 bite E (DP#24): only re-emit when declared --
                # None means "no principal sale" (the hold case, the golden
                # path), not a value to round-trip as an explicit null that a
                # naive from_dict() re-read would treat as "declared but
                # empty" (DP#32).
                **({'principal_sale': self.principal_sale}
                   if self.principal_sale is not None else {}),
            },
            'family': {
                'members': self.family_members,
                'children': self.children,
                'private_loans': self.private_loans,
                'gifts': self.gifts,
                'first_home_purchases': self.first_home_purchases,
            },
            'accounts': {
                'rrsp_annual_percent': self.rrsp_annual_percent,
                'rrsp_annual_max': self.rrsp_annual_max,
                'tfsa_annual_room_per_person': self.tfsa_annual_room_per_person,
                'resp_current_balance': self.resp_current_balance,
                'resp_study_start_age': self.resp_study_start_age,
                'resp_study_duration_years': self.resp_study_duration_years,
                'resp_used_for_education': self.resp_used_for_education,
                'resp_composition': self.resp_composition,
                'deduct_later_bracket_target': self.deduct_later_bracket_target,
                # Issue #823 (DP#24): round-trip the per-account override /
                # illiquidity maps so a load->modify->save cycle does not
                # silently drop them. Empty dict round-trips to 'absent'
                # (no override declared), the same absence-safe convention
                # used for lira / equity_grants above.
                **({'return_overrides': self.account_return_overrides}
                   if self.account_return_overrides else {}),
                **({'locked': self.account_locked}
                   if self.account_locked else {}),
                **({'mer_drag': self.account_mer_drag}
                   if self.account_mer_drag else {}),
            },
            'tax': {
                'country': self.country,
                'province': self.province,
            },
            'portfolio': self.portfolio_data,
            'retirement': self.retirement_data,
            'estate': self.estate_data,
            'return_model': self.return_model_data,
            # Issue #729 (DP#24): re-emit the top-level CRI/LIRA block so a
            # load->modify->save cycle does not silently drop locked-in
            # pension balances. Emitted only when non-empty -- an empty dict
            # round-trips to 'absent' (no LIRA declared), the same
            # absence-safe convention used for consumer_loans/installments/
            # equity_grants above. Before this, from_dict() ingested
            # cfg['lira'] but to_dict() never re-emitted it, so any saved
            # config lost its locked-in accounts on the next load (#293).
            **({'lira': self.lira_data} if self.lira_data else {}),
            # Issue #679: only re-emitted when actually declared -- None
            # means "never supplied" (DP#32), not a value to round-trip as
            # a fabricated 0. Issue #761: the discretionary_fraction travels
            # alongside living_costs when the household declared a split.
            **({'household_budget': {
                    'living_costs': self.living_costs,
                    **({'discretionary_fraction': self.discretionary_fraction}
                       if self.discretionary_fraction is not None else {}),
                    # Issue #760 (DP#24): the dated segments travel alongside
                    # living_costs when declared -- an empty list round-trips to
                    # "absent" (no dated segments), never a fabricated block.
                    **({'expense_segments': self.expense_segments}
                       if self.expense_segments else {})}}
               if self.living_costs is not None else {}),
            # Issue #763: only re-emitted when the household actually declared
            # consumer loans -- an empty list round-trips to "absent" (no
            # consumer debt), never to a fabricated block (DP#24/DP#32).
            **({'consumer_loans': self.consumer_loans}
               if self.consumer_loans else {}),
            # Issue #759: only re-emitted when the household actually declared
            # installment plans -- an empty list round-trips to "absent" (no
            # payment plan), never to a fabricated block (DP#24/DP#32).
            **({'installments': self.installments}
               if self.installments else {}),
            # Issue #768: only re-emitted when the household actually declared
            # equity grants -- an empty list round-trips to 'absent' (no
            # grants), never to a fabricated block (DP#24/DP#32).
            **({'equity_grants': self.equity_grants}
               if self.equity_grants else {}),
            # Issue #692: only re-emitted when the household actually declared a
            # couple-owned non-principal property -- an empty list round-trips
            # to 'absent' (no such property), never a fabricated block
            # (DP#24/DP#32).
            **({'properties': self.properties}
               if self.properties else {}),
            # Issue #936: the declared deposit products and the single taken
            # product are only re-emitted when actually present -- an empty list
            # / None round-trips to 'absent' (no product), never a fabricated block
            # (DP#24/DP#32).
            **({'deposit_products': self.deposit_products}
               if self.deposit_products else {}),
            **({'deposit_product': self.deposit_product}
               if self.deposit_product is not None else {}),
        }

    def to_json(self, path: str = None, indent: int = 2) -> str:
        """Serialize config to a JSON string, optionally writing it to disk."""
        return _dict_to_json(self.to_dict(), path=path, indent=indent)

    @classmethod
    def overlay_diff(cls, base: 'SimulationConfig', modified: 'SimulationConfig') -> Dict:
        """Compute the diff between a base config and a modified config (DP#18)."""
        base_dict = base.to_dict()
        mod_dict = modified.to_dict()
        overlays = {}

        def _deep_diff(base_d: Dict, mod_d: Dict, path: str = '') -> None:
            for key in set(list(base_d.keys()) + list(mod_d.keys())):
                current_path = f"{path}.{key}" if path else key
                bval = base_d.get(key)
                mval = mod_d.get(key)

                if isinstance(bval, dict) and isinstance(mval, dict):
                    _deep_diff(bval, mval, current_path)
                elif bval != mval:
                    overlays[current_path] = {
                        'from': bval,
                        'to': mval,
                    }

        _deep_diff(base_dict, mod_dict)

        return {
            'base_fields': len(base_dict),
            'overlays': overlays,
            'n_changes': len(overlays),
        }


# DP#13/DP#9: the round-number placeholder amortization used by the
# exploratory scripts (optimize.py's LTV/rate-anchor sweeps, output_plugins.py
# cash-flow reports) that sweep hypothetical LTV levels the household has not
# committed to -- as opposed to a *declared* refinance option, whose
# amortization is real data and always wins (DP#13's own distinction: a
# search/exploration parameter may default to a round, clearly-placeholder
# value; a declared fact may not). 25 years matches
# ``SimulationConfig.amortization_years``'s own DP#13 default for the
# incumbent mortgage. ``apply_ltv_overlay``/``apply_overlay`` themselves take
# NO such fallback (#655/DP#32): they require an explicit or config-declared
# amortization and refuse otherwise. This placeholder exists for ONE layer
# above them, so exploratory scripts keep working while still re-amortizing a
# refinance over a realistic new-loan term instead of the incumbent's
# remaining schedule.
REFINANCE_AMORTIZATION_PLACEHOLDER_YEARS = 25


def refinance_amortization_fallback(cfg: dict,
                                     placeholder: int = REFINANCE_AMORTIZATION_PLACEHOLDER_YEARS) -> int:
    """The refinance amortization an exploratory script should use: the
    config's own declared ``property.refinance_amortization_years`` if
    present, else the round DP#13 placeholder documented above. DP#9: the
    ONE place this fallback is computed, so every exploratory call site
    (optimize.py, output_plugins.py) agrees.
    """
    declared = cfg.get('property', {}).get('refinance_amortization_years')
    return declared if declared is not None else placeholder


def apply_overlay(base_cfg: dict, overlay: ScenarioOverlay) -> dict:
    """DP#18/DP#24: Apply a ScenarioOverlay to a base config dict, returning a derived config.

    Per DP#18, overlays modify - they don't replace. Income fields are only
    overridden when the overlay provides an explicit value (not None).

    The derived config is a deep copy of base_cfg with overlay fields applied.
    The overlay can be recovered via overlay.to_dict() for round-trip serialization (DP#24).

    Args:
        base_cfg: Base configuration dict (from input.json or SimulationConfig.to_dict()).
        overlay: ScenarioOverlay with delta values.

    Returns:
        Derived config dict with overlay applied.

    Raises:
        MissingRefinanceAmortizationError: overlay.cash_out > 0 (a refinance
            is being booked) but no refinance amortization is declared
            (issue #655; see apply_ltv_overlay's docstring).
        ChargeLimitExceededError: the resulting mortgage + HELOC would exceed
            the property's registered charge (issue #664; see
            apply_ltv_overlay's docstring).
    """
    cfg = deepcopy(base_cfg)

    # Override income - only if overlay explicitly sets it (DP#18: null means no change)
    for member in cfg['family']['members']:
        if member['role'] == 'primary' and overlay.primary_income is not None:
            member['gross_income'] = overlay.primary_income
        elif member['role'] == 'spouse' and overlay.spouse_income is not None:
            member['gross_income'] = overlay.spouse_income

    # Override mortgage rate
    cfg['property']['mortgage_rate'] = overlay.mortgage_rate

    # Property values
    house_value = cfg['property']['house_value']
    # Issue #1036: a mortgage-free household (no kind=mortgage liability) has
    # no `mortgage_balance` key in its internal property block --
    # input_contract.py omits it rather than writing 0 (DP#32: a missing
    # mortgage is a first-class state, not a zero mortgage). This overlay path
    # used to index the key directly and crash with KeyError on every overlay
    # (the LTV/cash-out exploration), so a mortgage-free household could not
    # run through main() at all. Treat the absent key as a $0 incumbent mortgage
    # -- the explicit-absence-test the rest of this function already uses for
    # margin_available (#663), never a truthiness coercion (DP#32).
    orig_mortgage = cfg['property']['mortgage_balance'] if 'mortgage_balance' in cfg['property'] else 0
    cash_out = overlay.cash_out

    # Money-flow model (issue #257): a cash-out refinance is a MORTGAGE increase.
    # The borrowed cash_out is added to mortgage_balance and its proceeds are
    # invested directly (sourced into the lump sum via property['cash_out']).
    #
    # issue #664: mortgage and HELOC are NOT independent borrowing sources --
    # on a readvanceable/all-in-one product they share ONE registered charge
    # with ONE combined limit. Booking `cash_out` as a mortgage advance
    # consumes exactly that much of the shared charge, so margin_available
    # (pre-existing undrawn HELOC room) shrinks dollar-for-dollar (never
    # below 0) rather than staying untouched -- leaving it untouched would
    # record the same borrowed dollar as debt twice (once as HELOC, once as
    # mortgage; #619's original finding, only partially fixed -- #619 zeroed
    # out the *inflation*, but nothing yet bounded the two combined against
    # the charge that backs them both).
    #
    # issue #663/DP#32: when there is no readvanceable facility at all,
    # base_cfg['property'] has no margin_available key (input_contract.py
    # deliberately omits it -- writing 0 would be the fallback DP#32
    # forbids). An overlay must not invent that key either: a "no HELOC"
    # household stays a "no HELOC" household through every overlay -- there
    # is simply no revolving room to shrink.
    if cash_out > 0:
        # issue #655: a cash-out refinance is a NEW LOAN, re-amortized over
        # its own declared term -- not the incumbent mortgage's remaining
        # amortization (which roughly doubles the implied payment on a
        # near-payoff mortgage). DP#32: the overlay's explicit value wins;
        # falling back to the base config's declared
        # property.refinance_amortization_years (sourced from
        # decisions.mortgage.refinance_options[].amortization_years);
        # absence of BOTH is refused rather than silently inheriting
        # property.amortization_years.
        amort = overlay.refinance_amortization_years
        if amort is None:
            amort = cfg['property'].get('refinance_amortization_years')
        if amort is None:
            raise MissingRefinanceAmortizationError(
                f"apply_overlay({overlay.label!r}) books a ${cash_out:,.0f} "
                f"cash-out refinance (mortgage ${orig_mortgage:,.0f} -> "
                f"${orig_mortgage + cash_out:,.0f}) but no refinance "
                f"amortization is declared. A refinance is a new loan -- "
                f"silently repaying it over the incumbent's remaining "
                f"{cfg['property'].get('amortization_years')}-year amortization "
                f"overstates the payment on a near-payoff mortgage by roughly "
                f"2x (#655). Set ScenarioOverlay.refinance_amortization_years, "
                f"or property.refinance_amortization_years on the base config."
            )
        cfg['property']['amortization_years'] = amort

        orig_margin = cfg['property'].get('margin_available')
        if orig_margin is not None:
            cfg['property']['margin_available'] = max(0.0, orig_margin - cash_out)
        margin_for_charge_check = cfg['property'].get('margin_available', 0.0)

        charge_ltv_limit = cfg['property'].get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
        limit = charge_limit(house_value, charge_ltv_limit)
        new_mortgage_balance = orig_mortgage + cash_out
        total_secured = new_mortgage_balance + margin_for_charge_check
        if total_secured > limit + _CHARGE_TOLERANCE:
            raise ChargeLimitExceededError(
                f"apply_overlay({overlay.label!r}) books ${new_mortgage_balance:,.0f} "
                f"mortgage + ${margin_for_charge_check:,.0f} HELOC room = "
                f"${total_secured:,.0f} secured debt "
                f"({total_secured / house_value:.0%} LTV, if house_value > 0) "
                f"against a ${limit:,.0f} charge limit ({charge_ltv_limit:.0%} "
                f"of ${house_value:,.0f} house value). Refused rather than "
                f"simulated (#664)."
            )

    cfg['property']['mortgage_balance'] = orig_mortgage + cash_out
    cfg['property']['cash_out'] = cash_out
    if overlay.ltv is not None:
        cfg['property']['ltv_max'] = overlay.ltv
    elif house_value > 0:
        cfg['property']['ltv_max'] = (orig_mortgage + cash_out) / house_value

    # RESP cash-out
    if overlay.resp_cash_out > 0:
        cfg['accounts']['resp_current_balance'] = 0
        if 'resp_composition' in cfg['accounts']:
            del cfg['accounts']['resp_composition']
        cfg['property']['free_cash'] = overlay.resp_cash_out

    if overlay.investment_return is not None:
        # DP#21 (#260/#591): route the swept rate through the one helper that
        # targets return_model, the engine's single source of truth.
        set_return_rate(cfg, overlay.investment_return)

    # DP#14/DP#5: Sensitivity overlay fields
    if overlay.inflation is not None:
        cfg.setdefault('assumptions', {})['inflation'] = overlay.inflation
    if overlay.salary_growth is not None:
        cfg.setdefault('assumptions', {})['salary_growth'] = overlay.salary_growth

    # Issue #303: retirement-age dimension. By default the swept age applies to
    # the PRIMARY member only (keeps the comparison simple — one moving part).
    # Set retirement.apply_to = "both" to move both adults' retirement_age in
    # lockstep when the household retires together.
    if overlay.retirement_age is not None:
        apply_to = (cfg.get('retirement', {}) or {}).get('apply_to', 'primary')
        for member in cfg['family']['members']:
            if apply_to == 'both' or member.get('role') == 'primary':
                member['retirement_age'] = overlay.retirement_age

    # Issues #711/#712: the CPP-sharing / pension-split elections land on the
    # keys the ENGINE reads -- SimulationConfig.from_dict maps cfg['retirement']
    # into SimulationConfig.retirement_data, which apply_retirement_income reads
    # as ret.get('cpp_share') / ret.get('pension_split_pct'). DP#18: writing them
    # anywhere else would be a dead-key sweep (the #591 shape). None => untouched.
    if overlay.cpp_share is not None:
        cfg.setdefault('retirement', {})['cpp_share'] = overlay.cpp_share
    if overlay.pension_split_pct is not None:
        cfg.setdefault('retirement', {})['pension_split_pct'] = overlay.pension_split_pct

    # Issue #688: the emergency-reserve target sweep (0/3/6/12/24 months).
    # DP#18: this must land on the key the ENGINE reads -- SimulationConfig
    # .from_dict maps `emergency_reserve.target_months` into
    # `SimulationConfig.emergency_reserve_target_months`, which SimState
    # .initial() sizes the cash sleeve from and simulation_rules
    # .apply_solvency draws first. A test that only asserts the merged CONFIG
    # changed would not prove that (#591's dead-key sweep is exactly this
    # shape); tests/test_issue_679_solvency.py asserts the ENGINE's OUTPUT
    # moves -- terminal wealth falls and the ruin outcome flips -- as the
    # target is swept.
    #
    # Sweeping a target onto a household that declared no reserve block at
    # all is a legitimate "what if I started holding one?" question, so the
    # block is created if absent -- but every other field it needs
    # (held_in/instrument/rate) has no answer the overlay could invent, so
    # the sweep is REFUSED rather than run against fabricated ones (DP#32).
    if overlay.emergency_reserve_months is not None:
        reserve_cfg = (cfg.get('assumptions', {}) or {}).get('emergency_reserve')
        if not reserve_cfg:
            raise ValueError(
                f"apply_overlay({overlay.label!r}) sweeps "
                f"emergency_reserve_months={overlay.emergency_reserve_months} "
                f"but the base config declares no assumptions.emergency_reserve "
                f"block -- so the reserve's instrument, its rate, and the "
                f"account it is held in are all unknown. A reserve swept "
                f"against an invented rate/location is not a sweep of the "
                f"household's real decision, it is a sweep of a fabricated "
                f"one (#688, DP#32). Declare the block (target_months may be "
                f"0) and sweep the target off it."
            )
        cfg['assumptions']['emergency_reserve'] = {
            **reserve_cfg, 'target_months': overlay.emergency_reserve_months}

    # Issue #936: the deposit product this scenario TAKES. DP#18: it must
    # land on the key the ENGINE reads -- SimulationConfig.from_dict maps
    # cfg['deposit_product'] into SimulationConfig.deposit_product, which
    # SimState.initial carves the fund_amount out of funding_source from
    # (money-conserving) and apply_deposit_product_growth grows at its
    # rate_schedule. None means the "leave it" baseline (the same money stays in
    # the funding source, growing at that account's rate) -- writing nothing
    # leaves the config in its no-product shape, byte-identical to today (DP#32). The
    # carve itself is done in SimState.initial (where the funding source's
    # opening balance is known), NOT here -- exactly as the emergency-reserve
    # sleeve (#688) carves in initial() rather than in the overlay.
    if overlay.deposit_product is not None:
        cfg['deposit_product'] = overlay.deposit_product

    # DP#24: Attach serializable overlay metadata for round-trip
    cfg['_overlay'] = overlay.to_dict()

    return cfg


def build_overlay_config(base_cfg: dict, overlay: ScenarioOverlay) -> dict:
    """DP#18: Overlay a delta on the base config, producing a derived scenario.

    Delegates to apply_overlay.
    """
    return apply_overlay(base_cfg, overlay)


# ═══════════════════════════════════════════════════════════════════════════
# Issue #1075: the 3-tranche readvanceable structure (house / deductible
# investment / line) -- the tranche-aware half of ``apply_structure_overlay``
# / ``apply_sourcing_overlay``. ONE spelling of the spec validation and the
# amount application, shared by the contract-load check, the two overlays and
# the optimizer's sweep points (DP#9).
# ═══════════════════════════════════════════════════════════════════════════

_TRANCHE_KINDS = ('house', 'investment', 'line')


def _validate_tranche_spec(structure: Dict) -> Dict:
    """Validate a structure's ``tranches`` declaration, returning it keyed by
    kind. DP#32: every refusal is loud and names the structure and the tranche
    -- an invalid spec must never be silently half-applied.

    Raises:
        ValueError: a spec the sweep could not honour -- overlapping kinds,
            a ``min_amount`` on a non-house tranche, a ``deductible`` flag on
            a non-investment tranche, an unpriced revolving segment, or a
            missing/empty ``tranches`` array. ``input_contract`` wraps this
            as ``ContractAdaptationError`` at contract-load time.
    """
    label = _structure_label(structure)
    tranches = structure.get('tranches')
    if not tranches:
        raise ValueError(
            f"structure {label!r} declares tranches but the tranches array is "
            f"empty -- nothing to apply (issue #1075)."
        )
    by_kind: Dict[str, Dict] = {}
    for t in tranches:
        kind = t.get('kind')
        if kind not in _TRANCHE_KINDS:
            raise ValueError(
                f"structure {label!r} declares a tranche of unknown kind "
                f"{kind!r} -- must be one of {list(_TRANCHE_KINDS)} (issue #1075)."
            )
        if kind in by_kind:
            raise ValueError(
                f"structure {label!r} declares TWO tranches of kind {kind!r} -- "
                f"the 3-tranche split has at most one sub-account per kind; "
                f"overlapping kinds cannot be priced (issue #1075)."
            )
        if 'min_amount' in t and kind != 'house':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a min_amount -- "
                f"only the 'house' tranche carries a floor (its amount is the "
                f"household's house mortgage, which is what a lender's cash-back "
                f"programs price); the investment and line amounts are swept "
                f"from the charge (issue #1075)."
            )
        if 'min_house_floor' in t and kind != 'house':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a "
                f"min_house_floor -- only the 'house' tranche carries a sweep "
                f"floor (its amount is the household's house mortgage, which is "
                f"what a lender's cash-back programs price); the investment and "
                f"line amounts are swept from the charge (issue #1075)."
            )
        if t.get('deductible') and kind != 'investment':
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares deductible: true "
                f"-- only the 'investment' tranche's interest is deductible "
                f"under ITA s.20(1)(c) (borrowed to invest); the house tranche "
                f"and the line are never deductible by declaration (issue #1075)."
            )
        if 'rate_type' in t and 'rate' not in t:
            raise ValueError(
                f"structure {label!r} tranche {kind!r} declares a rate_type but "
                f"no rate -- a rate_type without its rate is meaningless (issue "
                f"#1075)."
            )
        by_kind[kind] = t
    return by_kind


def _tranche_house_floor(by_kind: Dict, charge: float) -> float:
    """Issue #1075 (optimizer half): the house tranche amount's SWEEP FLOOR.

    ``min_house_floor`` when the house tranche declares one, else 60% of the
    registered charge (``_HOUSE_SWEEP_FLOOR_FRACTION`` -- DP#13: a fallback
    for absent input, never an opinion). This is NOT the ``min_amount``:
    that is the CASH-BACK THRESHOLD (the point the sweep reports the
    incentive at), and the whole point of this sweep is that the house
    mortgage may go BELOW it, forgoing the cash-back. The sweep enumerates
    house amounts from this floor UP to the charge (``_tranche_sweep_points``
    in optimize.py); ``_apply_tranched_structure`` refuses a split below it
    loudly (DP#32: never clamp, never silently no-op).

    A structure that declares no house tranche at all has no house mortgage
    to floor -- the sweep is over the investment/line split alone, house
    pinned at 0, exactly as before this dimension existed.
    """
    house_tranche = by_kind.get('house')
    if house_tranche is None:
        return 0.0
    declared_floor = house_tranche.get('min_house_floor')
    # DP#32: explicit presence test -- a declared floor of exactly 0 is a
    # real value (sweep the whole charge's worth of house room), never
    # shadowed by the fallback.
    if declared_floor is not None:
        return declared_floor
    return charge * _HOUSE_SWEEP_FLOOR_FRACTION


def _tranche_line_rate(structure: Dict, by_kind: Dict) -> tuple:
    """The revolving segment's (rate, rate_type) for a tranches-declared
    structure: the 'line' tranche's own pair, else the structure-level
    ``revolving_rate``/``revolving_rate_type`` (the #687 spelling), else None.
    Raises ValueError when the structure can carry a line balance -- today
    (line amount > 0) or later (readvanceable) -- and no pair prices it
    (#654, DP#32: never derived from the mortgage's own rate)."""
    label = _structure_label(structure)
    line = by_kind.get('line')
    # DP#32: explicit None-testing -- a declared rate of exactly 0.0 is a real
    # value, never shadowed by the fallback (and `or` would make 0
    # unrepresentable, the exact #654 trap).
    rate = None
    if line is not None and line.get('rate') is not None:
        rate = line['rate']
    elif structure.get('revolving_rate') is not None:
        rate = structure['revolving_rate']
    rate_type = None
    if line is not None and line.get('rate_type') is not None:
        rate_type = line['rate_type']
    elif structure.get('revolving_rate_type') is not None:
        rate_type = structure['revolving_rate_type']
    if (rate is None) != (rate_type is None):
        raise ValueError(
            f"structure {label!r} prices its revolving line with a rate but no "
            f"rate_type (or vice versa) -- a line that can carry a balance must "
            f"declare both, and it is never derived from the mortgage's own "
            f"rate (#654/#1075)."
        )
    return rate, rate_type


def _apply_tranched_structure(property_cfg: dict, structure: Dict) -> dict:
    """Issue #1075: apply a 3-tranche structure (house / deductible investment
    / line) to a ``property`` dict, returning a derived copy.

    The tranche spec declares the KINDS and the FIXED facts (the house
    tranche's SWEEP FLOOR -- ``min_house_floor``, defaulting to 60% of the
    charge, NOT the cash-back threshold, which the sweep may go below -- the
    investment tranche's deductibility, each tranche's own rate); the
    AMOUNTS come from ``structure['tranche_amounts']`` -- a concrete
    ``{house, investment, line}`` split summing to the registered charge, as
    the optimizer's sweep enumerates. The amounts are the OPENING POSITION,
    not a re-split of a carried-through drawn balance (#851's invariant
    applies to the share form): the household's drawn mortgage IS house +
    investment (the investment tranche was borrowed to invest), the undrawn
    room IS the line, and the deductible investment tranche's EXACT interest
    (balance x its OWN rate -- never the blended-rate product, which drags
    the house tranche's cheaper rate in) is carried on
    ``deductible_mortgage_balance``/``deductible_mortgage_interest`` for the
    s.20(1)(c) pricing.

    Without ``tranche_amounts`` (the contract-load check's call), the
    MINIMUM point is applied instead -- house at its SWEEP FLOOR (the
    smallest amount the sweep will ever enumerate; ``min_amount`` is NOT
    consulted -- it is the cash-back threshold, and going below it is the
    point of the sweep), investment 0, the whole rest of the charge as the
    line. That is the binding point for both OSFI B-20 ceilings (the
    largest possible revolving segment; the combined cap cannot bind since
    the split sums to the charge by construction), so a structure that
    passes it passes every sweep point -- and one whose floor exceeds the
    charge fails loudly here, at contract load, rather than when a sweep
    happens to reach it (DP#32).

    Money is conserved by construction: the three amounts partition the
    charge exactly, and the engine books mortgage debt for house + investment
    and (when drawn) the line -- every borrowed dollar once (DP#18).

    Raises:
        ValueError: an invalid tranche spec (``_validate_tranche_spec``).
        ChargeLimitExceededError: the house floor exceeds the charge, or the
            split breaches either OSFI B-20 ceiling (the 80% combined cap or
            the 65% revolving-only cap), or the amounts do not sum to the
            charge -- refused rather than silently clamped (DP#32).
    """
    property_cfg = deepcopy(property_cfg)
    by_kind = _validate_tranche_spec(structure)
    label = _structure_label(structure)

    house_value = property_cfg.get('house_value', 0.0)
    orig_mortgage = property_cfg.get('mortgage_balance', 0.0)
    # DP#32: explicit absence-testing, never `x or 0` -- margin_available
    # absent means "no facility at all" (#663), a different state from a
    # declared facility with $0 room, but its contribution to the charge is
    # 0 either way, which is all the arithmetic below needs.
    orig_margin = property_cfg.get('margin_available')
    orig_margin = 0.0 if orig_margin is None else orig_margin
    charge = orig_mortgage + orig_margin

    house_tranche = by_kind.get('house')
    investment_tranche = by_kind.get('investment')
    house_floor = _tranche_house_floor(by_kind, charge)

    amounts = structure.get('tranche_amounts')
    if amounts is None:
        # Contract-load feasibility check: the minimum point (house at its
        # SWEEP FLOOR -- the smallest amount the sweep will ever enumerate;
        # ``min_amount`` is the cash-back threshold and is NOT a floor, see
        # ``_tranche_house_floor`` -- no investment, the whole remainder as
        # the line: the binding revolving-cap case).
        if house_floor > charge + _CHARGE_TOLERANCE:
            raise ChargeLimitExceededError(
                f"structure {label!r}: its house sweep floor of "
                f"${house_floor:,.0f} exceeds the ${charge:,.0f} registered charge "
                f"-- there is no house amount between the floor and the charge, "
                f"so no 3-tranche split exists to sweep (issue #1075)."
            )
        amounts = {'house': house_floor, 'investment': 0.0,
                   'line': max(0.0, charge - house_floor)}

    house = amounts['house']
    investment = amounts['investment']
    line = amounts['line']

    if house < house_floor - _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: its house tranche amount of ${house:,.0f} "
            f"is below the ${house_floor:,.0f} sweep floor (the declared "
            f"min_house_floor, defaulting to 60% of the registered charge) -- "
            f"the sweep varies the house mortgage from its floor up to the "
            f"charge, and a split below the floor is not a candidate (issue "
            f"#1075)."
        )
    if abs((house + investment + line) - charge) > _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: its tranche amounts ${house:,.0f} (house) + "
            f"${investment:,.0f} (investment) + ${line:,.0f} (line) = "
            f"${house + investment + line:,.0f} do not sum to the "
            f"${charge:,.0f} registered charge -- the 3-tranche split is a "
            f"partition of ONE charge, and an amount above (or below) it is a "
            f"borrowed dollar that exists twice (or not at all) (issue #1075)."
        )

    line_rate, line_rate_type = _tranche_line_rate(structure, by_kind)
    readvanceable = bool(structure.get('readvanceable', False))
    if (line > 0 or readvanceable) and (line_rate is None or line_rate_type is None):
        raise ValueError(
            f"structure {label!r} carries a revolving line (${line:,.0f} of "
            f"room{', readvanceable' if readvanceable else ''}) but prices it "
            f"nowhere -- neither the 'line' tranche nor the structure-level "
            f"revolving_rate/revolving_rate_type. A line that can draw -- "
            f"directly, or later via readvance -- must be priced (#654/#1075); "
            f"it is never derived from the mortgage's own rate."
        )

    baseline_rate = property_cfg.get('mortgage_rate', 0.05)
    house_rate = house_tranche.get('rate') if (house_tranche and house_tranche.get('rate') is not None) else baseline_rate
    investment_rate = (investment_tranche.get('rate')
                       if (investment_tranche and investment_tranche.get('rate') is not None)
                       else baseline_rate)

    new_mortgage = house + investment
    new_margin = line
    if new_mortgage > 0:
        # The single amortization schedule is exact on total interest at the
        # balance-weighted rate (task #1075 data model); the per-tranche
        # EXACT interest is carried separately for the s.20(1)(c) pricing.
        blended_rate = (house * house_rate + investment * investment_rate) / new_mortgage
    else:
        blended_rate = baseline_rate

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)
    _refuse_structure_over_charge(
        structure, (line / charge) if charge > 0 else 0.0, house_value,
        charge, new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    property_cfg['mortgage_rate'] = blended_rate
    if investment > 0 and investment_tranche is not None and investment_tranche.get('deductible'):
        property_cfg['deductible_mortgage_balance'] = investment
        property_cfg['deductible_mortgage_interest'] = investment * investment_rate
    else:
        # DP#32: a split with no deductible tranche carries no deductible
        # keys -- never a fabricated zero.
        property_cfg.pop('deductible_mortgage_balance', None)
        property_cfg.pop('deductible_mortgage_interest', None)

    facility = dict(structure)
    facility['revolving_rate'] = line_rate
    facility['revolving_rate_type'] = line_rate_type
    _write_structure_facility(property_cfg, facility, new_margin)
    return property_cfg


def apply_structure_overlay(property_cfg: dict, structure: Dict) -> dict:
    """Issue #687: apply ONE candidate mortgage STRUCTURE
    (``decisions.mortgage.structure_options[]``) to a ``property`` config
    dict, returning a derived copy.

    A household facing a refinance/renewal decision may be choosing between
    genuinely different structures against the SAME registered charge --
    the whole charge as a plain amortizing mortgage, the same amount but
    flagged readvanceable (the line starts at $0 and grows only as
    principal is repaid), or a smaller mortgage plus an undrawn revolving
    line carved out today. ``structure['revolving_share']`` is the fraction
    of the CURRENT registered charge (``mortgage_balance + margin_available``)
    this structure sets aside as the revolving segment; the rest is available
    as amortizing mortgage. The charge total is UNCHANGED -- this overlay only
    changes how the same charge is SPLIT, never how much of it is drawn (that
    is ``apply_overlay``'s ``cash_out``, a separate, composable dimension).

    Issue #1075: a structure may instead declare ``tranches`` -- the
    3-tranche readvanceable form (house >= a declared minimum, deductible
    investment mortgage, readvanceable line) -- in which case the split is
    applied by ``_apply_tranched_structure`` below: the tranche AMOUNTS
    define the drawn/room position directly (mortgage = house + investment,
    undrawn room = line), and the deductible investment tranche's EXACT
    interest (balance x its OWN rate) is carried on
    ``deductible_mortgage_balance``/``deductible_mortgage_interest`` for the
    s.20(1)(c) pricing. The #687 share semantics above remain byte-for-byte
    for a structure that declares ``revolving_share`` (DP#13: the tranche
    spec is an additive opt-in).

    Issue #851: ``mortgage_balance`` is a DRAWN balance and ``margin_available``
    is UNDRAWN room -- two different things that must not be conflated. The
    drawn position (``mortgage_balance``) is carried through UNCHANGED; only the
    revolving segment (``margin_available`` = ``charge * revolving_share``, all
    undrawn on this no-cash-out path) is re-derived. So the household's total
    booked DEBT is the same for every ``revolving_share`` -- carving out a line
    that stays undrawn cannot invent or destroy a dollar of debt. This is the
    same drawn/room separation ``apply_sourcing_overlay`` (#845) applies at a
    positive ``cash_out``; this function is its ``cash_out``-of-0 case. (Before
    #851 this booked ``total_secured - new_margin`` as ``mortgage_balance``,
    which counted undrawn HELOC room as mortgage debt -- +$150,000 of phantom
    debt on #687's shipped line-free example.)

    ``structure.get('revolving_share')`` of ``None`` means "this structure
    dict carries no declared split" -- the identity/baseline case
    (``scenario_discovery``'s single-element fallback when
    ``decisions.mortgage.structure_options`` was never declared) -- and
    ``property_cfg`` is returned untouched, DP#13/DP#32: absence is not an
    opinion.

    Per DP#32/#663 (``has_readvanceable_facility``'s own docstring): "no
    revolving facility at all" and "a facility with $0 room" are NOT the
    same state. A structure with no revolving component AT ALL (revolving
    share 0.0 and not flagged readvanceable -- issue #687's structure A,
    "all_mortgage") clears ``margin_available``/``heloc_readvance``/
    ``heloc_rate``/``heloc_rate_type`` outright, rather than leaving a
    ``margin_available: 0`` key that would misreport a facility that
    structurally does not exist (and would spuriously trip
    ``resolve_heloc_rate``'s "declare a facility, declare its rate"
    warning for a structure that never asked for one). A structure that DOES
    carry a revolving component -- ``revolving_share > 0``, OR flagged
    ``readvanceable`` even at a $0 starting share (issue #687's structure B:
    the readvance mechanism, #664/#681, grows the line from $0 as principal
    is repaid) -- sets the facility fields for real, including
    ``heloc_rate``/``heloc_rate_type`` from the structure's OWN declared
    ``revolving_rate``/``revolving_rate_type`` (never derived from the
    mortgage's rate, #654).

    Raises:
        ChargeLimitExceededError: this structure's combined secured debt
            would exceed the registered charge (80% LTV, OSFI B-20), or its
            revolving segment ALONE would exceed the revolving-only ceiling
            (65% LTV, independent of the combined cap) -- refused rather
            than silently clamped (DP#32).
    """
    property_cfg = deepcopy(property_cfg)
    # Issue #1075: a structure that declares ``tranches`` (the 3-tranche
    # readvanceable form -- house / deductible investment / line) is applied
    # by the tranche machinery, NOT the share machinery: the tranches define
    # the split directly (and carry the deductible-tranche facts the share
    # form cannot express). See ``_apply_tranched_structure``.
    if structure.get('tranches') is not None:
        return _apply_tranched_structure(property_cfg, structure)

    revolving_share = structure.get('revolving_share')
    if revolving_share is None:
        return property_cfg

    house_value = property_cfg.get('house_value', 0.0)
    orig_mortgage = property_cfg.get('mortgage_balance', 0.0)
    orig_margin = property_cfg.get('margin_available', 0.0)
    total_secured = orig_mortgage + orig_margin

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)

    # Issue #851: ``revolving_share`` allocates the CHARGE into a revolving
    # segment, but ``mortgage_balance`` is a DRAWN balance (debt owed,
    # amortized, interest accrued) while ``margin_available`` is UNDRAWN room.
    # ``new_mortgage`` must stay the drawn position (``orig_mortgage``), never
    # ``total_secured - new_margin`` -- summing the drawn mortgage and the
    # undrawn line into ``total_secured`` and re-splitting it booked the
    # household's undrawn HELOC room as mortgage debt (the phantom debt #577
    # exists to prevent). The error was ``orig_margin * (1 - share) -
    # orig_mortgage * share``: it INVENTS debt at low shares (+150,000 on the
    # #687 shipped example's line-free structure A) and DESTROYS it at high
    # ones, and is not equal across structures -- systematically favouring
    # carving out a line, exactly the conclusion the #687 structure table
    # exists to test. This is ``apply_sourcing_overlay``'s drawn/room
    # separation at cash_out=0 (the no-cash-out path this overlay owns): total
    # booked debt stays ``orig_mortgage`` for EVERY share, and any revolving
    # segment carved out is UNDRAWN room, costing nothing until drawn.
    new_margin = total_secured * revolving_share
    new_mortgage = orig_mortgage

    _refuse_structure_over_charge(
        structure, revolving_share, house_value, total_secured,
        new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    _write_structure_facility(property_cfg, structure, new_margin)
    return property_cfg


def _structure_label(structure: Dict) -> str:
    """The name a refusal message calls this structure by (DP#32: a refusal
    must name the option it refused, never a bare index)."""
    label = structure.get('label')
    if label is None:
        label = structure.get('id', '?')
    return label


def _refuse_structure_over_charge(structure: Dict, revolving_share: float,
                                  house_value: float, total_secured: float,
                                  new_mortgage: float, new_margin: float,
                                  combined_max: float, revolving_max: float,
                                  charge_ltv_limit: float,
                                  heloc_ltv_limit: float) -> None:
    """Both OSFI B-20 refusals for ONE candidate structure (#664/#687).

    DP#9: extracted so ``apply_structure_overlay`` and
    ``apply_sourcing_overlay`` (#845/#849) enforce the SAME two ceilings
    from one implementation -- a second copy is exactly how #619's two
    private ``_apply_ltv_overlay``s came to agree with each other and
    disagree with the engine.
    """
    label = _structure_label(structure)
    if total_secured > combined_max + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: ${new_mortgage:,.0f} mortgage + "
            f"${new_margin:,.0f} revolving = ${total_secured:,.0f} secured "
            f"debt exceeds the ${combined_max:,.0f} charge limit "
            f"({charge_ltv_limit:.0%} of ${house_value:,.0f} house value) "
            f"-- OSFI B-20's legal maximum LTV for an uninsured combined "
            f"loan plan. Refused rather than simulated (#664/#687)."
        )
    if new_margin > revolving_max + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"structure {label!r}: ${new_margin:,.0f} revolving segment "
            f"({revolving_share:.0%} of ${total_secured:,.0f} secured debt) "
            f"exceeds the ${revolving_max:,.0f} revolving-only ceiling "
            f"({heloc_ltv_limit:.0%} of ${house_value:,.0f} house value) -- "
            f"OSFI B-20 caps the readvanceable/revolving segment of a "
            f"combined loan plan independent of the 80% combined cap; "
            f"lending above it must be amortizing and non-readvanceable. "
            f"Refused rather than simulated (#664/#687)."
        )


def _write_structure_facility(property_cfg: dict, structure: Dict,
                              new_margin: float) -> None:
    """Write (or clear) the revolving-facility fields this structure implies.

    DP#9/DP#32: shared by ``apply_structure_overlay`` and
    ``apply_sourcing_overlay``. Mutates ``property_cfg`` in place -- both
    callers own a fresh deep copy already.
    """
    readvanceable = bool(structure.get('readvanceable', False))
    if new_margin > 0 or readvanceable:
        property_cfg['margin_available'] = new_margin
        property_cfg['heloc_readvance'] = readvanceable
        if structure.get('revolving_rate') is not None:
            property_cfg['heloc_rate'] = structure['revolving_rate']
        if structure.get('revolving_rate_type') is not None:
            property_cfg['heloc_rate_type'] = structure['revolving_rate_type']
    else:
        # Issue #687's structure A ("all_mortgage"): no revolving component
        # at all -- clear any facility this property dict inherited from
        # the base config, rather than leave stale heloc facts an
        # 'all_mortgage' household did not choose (DP#18).
        property_cfg.pop('margin_available', None)
        property_cfg.pop('heloc_readvance', None)
        property_cfg.pop('heloc_rate', None)
        property_cfg.pop('heloc_rate_type', None)


def apply_sourcing_overlay(property_cfg: dict, structure: Dict) -> dict:
    """Issues #845/#849: apply ONE candidate structure to a property that has
    ALREADY had a cash-out refinance booked on it by ``apply_overlay``.

    This is the composition ``apply_structure_overlay`` cannot do. That
    function re-splits ``mortgage_balance + margin_available`` and books the
    WHOLE split as ``mortgage_balance`` debt -- correct when the property's
    combined secured position is entirely drawn, wrong the moment
    ``apply_overlay`` has just advanced ``cash_out`` and left undrawn room
    beside it. Composing the two naively books the leftover room as mortgage
    debt (money from nowhere) and, at ``draw_fraction > 0``, invests the same
    borrowed dollar twice (``lump_sum = margin_available * draw_fraction +
    cash_out``, ``optimizer.py``). Measured on ``schema/example.json``'s own
    numbers, the naive composition mis-booked $100,000 of debt.

    The question this DOES answer -- the irreversible notary-day one #849
    opens with -- is **where the surplus comes from at a FIXED registered
    charge**:

      - ``revolving_share = 0.0``  -> the whole surplus is an amortizing
        MORTGAGE ADVANCE (cheaper rate; forced principal repayment erodes the
        deductible balance);
      - ``revolving_share >= cash_out / charge`` -> the whole surplus is a
        REVOLVING DRAW (dearer rate; interest-only, so the deductible balance
        does not amortize away);
      - in between -> the surplus is split, line first.

    ``cash_out`` can only say HOW MUCH; only ``revolving_share`` can say FROM
    WHERE (#849). So:

      charge          = mortgage_balance + margin_available  (post-refinance;
                        apply_overlay already booked the advance on the
                        mortgage and shrank the pre-existing room by it)
      drawn           = mortgage_balance  (every borrowed dollar, booked once)
      revolving       = charge * revolving_share
      line_draw       = min(cash_out, revolving)   <- the surplus, line first
      mortgage_balance = drawn - line_draw

    Money is conserved by construction, for EVERY share: the household still
    owes exactly ``drawn``, and ``optimizer.py`` still invests exactly
    ``cash_out`` (``margin_available * 0.0 + cash_out``) while booking
    ``margin_draw_for_lump_sum(cash_out, revolving) = line_draw`` of it as
    HELOC debt. That is why ``run_mortgage_structure_exploration`` pins the
    draw fraction to 0.0 on a cash-out basis: at a fixed charge the draw is
    IMPLIED by the sourcing split, and sweeping it again would invest
    borrowed money twice. Any residual room (``revolving - line_draw``, when
    the structure carves out a line larger than the surplus) stays undrawn --
    real standby liquidity, correctly costing nothing.

    ``revolving_share`` of ``None`` (the no-structure-declared identity case)
    returns ``property_cfg`` untouched, same as ``apply_structure_overlay``
    (DP#13/DP#32).

    Raises:
        ChargeLimitExceededError: same two OSFI B-20 ceilings
            ``apply_structure_overlay`` enforces, from the same helper (DP#9).
    """
    property_cfg = deepcopy(property_cfg)
    # Issue #1075: a tranches-declared structure is applied by the tranche
    # machinery, not the share machinery -- see ``apply_structure_overlay``'s
    # identical routing. For the tranched form the sweep point's AMOUNTS
    # already ARE the post-refinance drawn/room split (the investment
    # tranche is the advance; the line holds the rest as room), so the
    # sourcing line-draw re-split does not apply -- the year-0 lump-sum
    # machinery (``margin_draw_for_lump_sum``) still prices how much of the
    # surplus is drawn from the line when a cash-out is invested.
    if structure.get('tranches') is not None:
        return _apply_tranched_structure(property_cfg, structure)

    revolving_share = structure.get('revolving_share')
    if revolving_share is None:
        return property_cfg

    house_value = property_cfg.get('house_value', 0.0)
    drawn = property_cfg.get('mortgage_balance', 0.0)
    # DP#32: explicit absence-testing, never `x or 0`. `margin_available`
    # absent means "no facility at all" (#663) -- a DIFFERENT state from a
    # declared facility with $0 room -- but the charge it contributes is 0
    # either way, which is the only thing the arithmetic below needs. A
    # `cash_out` of 0 is likewise a real value ("this option advances
    # nothing"), not an unset one.
    room = property_cfg.get('margin_available')
    room = 0.0 if room is None else room
    charge = drawn + room
    cash_out = property_cfg.get('cash_out')
    cash_out = 0.0 if cash_out is None else cash_out

    charge_ltv_limit = property_cfg.get('charge_ltv_limit', OSFI_B20_CHARGE_LTV_MAX)
    heloc_ltv_limit = property_cfg.get('heloc_ltv_limit', OSFI_B20_REVOLVING_LTV_MAX)
    combined_max = charge_limit(house_value, charge_ltv_limit)
    revolving_max = heloc_revolving_limit(house_value, heloc_ltv_limit)

    new_margin = charge * revolving_share
    line_draw = min(cash_out, new_margin)
    new_mortgage = drawn - line_draw

    _refuse_structure_over_charge(
        structure, revolving_share, house_value, charge,
        new_mortgage, new_margin, combined_max, revolving_max,
        charge_ltv_limit, heloc_ltv_limit)

    property_cfg['mortgage_balance'] = new_mortgage
    _write_structure_facility(property_cfg, structure, new_margin)
    return property_cfg


def apply_property_funding_overlay(cfg: dict, assignment: Dict[str, Dict]) -> dict:
    """Issue #1011: apply ONE candidate funding ASSIGNMENT to a config,
    returning a derived copy.

    ``assignment`` maps a property id to the funding OPTION that cell scores
    (one entry of ``scenario_discovery.discover_property_funding_cells``'s
    ``assignment``). For each named property, this REBUILDS the property's
    ``purchase.financing`` block (or removes it) and recomputes its
    ``net_equity``/``secured_share`` so the down payment the waterfall draws
    and the originated mortgage the fold services both reflect the chosen
    funding method -- then re-optimisation ranks each cell's result by the
    active objective (DP#22).

    ``all_cash``: no originated mortgage; ``net_equity`` becomes the full
    couple-share value (less any year-0 secured debt), so the whole value
    leaves the portfolio as the down payment -- byte-identical to #696's
    equity-financed behaviour.

    ``mortgage``: ``down_pct`` of value is the down payment (``net_equity``)
    drawn through the waterfall and a mortgage originates for the rest
    (``value * (1 - down_pct)``, couple share), serviced from the purchase
    year to payoff, interest deductible when the property is a rental. The
    financing block is built from the SAME ``_annual_amortization_schedule``
    the fixed-``financing`` mapper uses (DP#9: one schedule spelling), and
    the deductibility follows the property's ``kind`` -- exactly the rule
    ``contract_property._map_owned_properties`` applies to a fixed ``financing``.

    The recompute inputs (``value_share``, the non-financing ``secured_base``,
    ``owner_roles``, ``deductible``, ``projection_years``) are carried on the
    property's ``purchase.funding_recompute`` block by the mapper precisely so
    this overlay does NOT re-derive the owner structure or the horizon here
    (DP#9). A property the assignment does NOT name is left untouched -- a
    multi-property cross only changes the properties the cell chooses for.

    Absence-safe (DP#32): the base ``cfg`` a household with no
    ``funding_options`` loads carries no ``funding_recompute`` and is never
    passed through here -- the exploration is gated on a declaration, so the
    golden trajectory never reaches this function.
    """
    from contract_property import _annual_amortization_schedule

    cfg = deepcopy(cfg)
    props = cfg.get('properties', [])
    for prop in props:
        pid = prop.get('id')
        if pid not in assignment:
            continue
        option = assignment[pid]
        purchase = prop.get('purchase')
        if purchase is None or 'funding_recompute' not in purchase:
            # Not a fundable purchase (no declaration carried a recompute
            # block). Leave it -- the assignment named a property this cell
            # cannot refund, which is a caller bug, not a silent no-op, but
            # crashing mid-sweep would lose every other cell's result, so we
            # skip the named-but-not-fundable property and let the caller's
            # absence tests catch the wiring gap.
            continue
        rc = purchase['funding_recompute']
        value_share = prop['value_share']
        secured_base = rc['secured_base']
        pyear = purchase['year']
        method = option['method']
        if method == 'mortgage':
            financed_share = value_share * (1.0 - option['down_pct'])
            schedule = _annual_amortization_schedule(
                financed_share, option['rate'],
                option['amortization_years'], pyear,
                rc['projection_years'])
            purchase['financing'] = {
                'mortgage_amount': financed_share,
                'rate': option['rate'],
                'rate_type': option['rate_type'],
                'amortization_years': option['amortization_years'],
                'origination_year': pyear,
                'deductible': rc['deductible'],
                'owner_roles': rc['owner_roles'],
                'schedule': schedule,
            }
            new_secured = secured_base + financed_share
            # A rental's interest deduction reads the financing schedule off
            # the rental block (mirroring _map_owned_properties); keep the
            # reference in sync with the block we just materialized.
            rental = prop.get('rental')
            if rental is not None:
                rental['financing_schedule'] = schedule
        elif method == 'all_cash':
            purchase.pop('financing', None)
            new_secured = secured_base
            rental = prop.get('rental')
            if rental is not None:
                rental.pop('financing_schedule', None)
        else:
            # A method the schema's enum does not list cannot reach here from a
            # validated document, but a future schema addition that forgets an
            # overlay branch must NOT silently fall through to the unfunded
            # identity (the DP#32 trap: an unknown funding method defaulting to
            # the favourable "do nothing"). Fail loudly instead -- covered by
            # test_issue_1011's direct-overlay unknown-method test.
            raise ValueError(
                f"unknown funding method {method!r} for property {pid!r}")
        prop['secured_share'] = new_secured
        prop['net_equity'] = value_share - new_secured
    return cfg


def apply_ltv_overlay(config: 'SimulationConfig', ltv: float,
                       refinance_amortization_years: Optional[int] = None) -> 'SimulationConfig':
    """Overlay a target LTV onto a base SimulationConfig (DP#18, issues
    #257/#619/#664/#655).

    This is the ONE implementation of the LTV/cash-out overlay for the
    SimulationConfig-object callers -- GridOptimizer and ScipyOptimizer both
    call this instead of each maintaining their own copy (DP#9). It applies
    the same money-flow rule as ``apply_overlay`` above (the dict/
    ScenarioOverlay path used by simulate.py):

      - The cash-out needed to reach ``ltv`` is a MORTGAGE increase, booked
        as debt exactly once, on ``mortgage_balance``.
      - issue #664: a readvanceable mortgage and its HELOC are carved out of
        ONE registered charge with ONE combined limit -- NOT independent
        borrowing sources. ``margin_available`` (pre-existing undrawn HELOC
        room) shrinks dollar-for-dollar by the cash-out booked here (floored
        at 0), because that room is drawn from the exact same charge the
        mortgage advance just consumed. The resulting total secured debt
        (new mortgage balance + remaining margin_available) is asserted to
        fit inside ``charge_limit(house_value, charge_ltv_limit)``
        (OSFI B-20: 80% LTV is the legal maximum for an uninsured combined
        loan plan) -- a target LTV (or a pre-existing declared facility) that
        would breach it raises ``ChargeLimitExceededError`` rather than being
        silently simulated at >100% LTV.
      - issue #655: a cash-out refinance is a NEW LOAN, re-amortized over its
        own term -- not the incumbent mortgage's remaining amortization
        (which would roughly double the implied payment on a near-payoff
        mortgage). The new amortization comes from
        ``refinance_amortization_years`` if supplied, else
        ``config.refinance_amortization_years`` (sourced from a contract's
        ``decisions.mortgage.refinance_options[].amortization_years``); if
        neither is available, raises ``MissingRefinanceAmortizationError``
        (DP#32) rather than silently inheriting ``config.amortization_years``.
      - ``cash_out`` is recorded on the returned config so callers can size
        the invested lump sum as ``margin_available * draw_fraction +
        cash_out`` (issue #735: the margin draw is a swept decision,
        defaulting to 0.0/undrawn -- see ``GridOptimizer.optimize()``'s
        ``draw_fraction_options`` -- while the refinance proceeds are
        always fully realized) instead of recomputing it against an
        already-inflated margin.

    Prior to #619, ``optimizer.py`` and ``scipy_optimizer.py`` each carried a
    private ``_apply_ltv_overlay`` that inflated ``margin_available`` by
    ``cash_out`` -- the pre-#257 behaviour. The two copies agreed with each
    other (a test pinned them together) but both disagreed with this
    function, so every LTV > 0 optimizer result invested a full extra
    ``cash_out`` of capital that was never borrowed. #619 fixed the
    inflation, but left ``margin_available`` untouched by the overlay
    entirely -- so a household could still draw its FULL pre-existing HELOC
    limit *in addition to* a mortgage refinanced up to the same charge,
    doubling apparent borrowing capacity (#664).

    At LTV <= 0, or when the target LTV implies no cash-out (current LTV
    already meets or exceeds the target), returns config unchanged.

    Raises:
        MissingRefinanceAmortizationError: a cash-out is being booked but no
            refinance amortization is declared or supplied (#655).
        ChargeLimitExceededError: the resulting mortgage + HELOC would
            exceed the property's registered charge (#664).
    """
    if ltv <= 0:
        return config
    cash_out = max(0, ltv * config.house_value - config.mortgage_balance)
    if cash_out <= 0:
        return config

    amort = refinance_amortization_years
    if amort is None:
        amort = config.refinance_amortization_years
    if amort is None:
        raise MissingRefinanceAmortizationError(
            f"apply_ltv_overlay(ltv={ltv:.2%}) books a ${cash_out:,.0f} "
            f"cash-out refinance (mortgage ${config.mortgage_balance:,.0f} -> "
            f"${config.mortgage_balance + cash_out:,.0f}) but no refinance "
            f"amortization is declared. A refinance is a new loan -- "
            f"silently repaying it over the incumbent's remaining "
            f"{config.amortization_years}-year amortization overstates the "
            f"payment on a near-payoff mortgage by roughly 2x (#655). Set "
            f"config.refinance_amortization_years (mapped from "
            f"decisions.mortgage.refinance_options[].amortization_years), or "
            f"pass refinance_amortization_years= explicitly."
        )

    new_mortgage_balance = config.mortgage_balance + cash_out
    new_margin_available = max(0.0, config.margin_available - cash_out)

    overlaid = replace(config,
                        mortgage_balance=new_mortgage_balance,
                        cash_out=cash_out,
                        margin_available=new_margin_available,
                        amortization_years=amort,
                        ltv_max=ltv)

    total_secured = overlaid.mortgage_balance + overlaid.margin_available
    limit = charge_limit(overlaid.house_value, overlaid.charge_ltv_limit)
    if total_secured > limit + _CHARGE_TOLERANCE:
        raise ChargeLimitExceededError(
            f"apply_ltv_overlay(ltv={ltv:.2%}) books ${overlaid.mortgage_balance:,.0f} "
            f"mortgage + ${overlaid.margin_available:,.0f} HELOC room = "
            f"${total_secured:,.0f} secured debt "
            f"({total_secured / overlaid.house_value:.0%} LTV) against a "
            f"${limit:,.0f} charge limit ({overlaid.charge_ltv_limit:.0%} of "
            f"${overlaid.house_value:,.0f} house value). Refused rather than "
            f"simulated (#664)."
        )
    return overlaid
