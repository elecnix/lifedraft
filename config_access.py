#!/usr/bin/env python3
"""Readers, resolvers, and the shape guard for the RAW config dict.

Split out of ``simulation_config.py``. Everything here operates on the plain
``dict`` form of a configuration -- BEFORE it becomes a ``SimulationConfig``,
and after ``to_dict()`` turns one back into a dict -- so it is the layer both
``SimulationConfig`` itself and the overlay machinery share:

  - ``_validate_internal_shape`` + the two allowed-key sets: the internal-shape
    guard that refuses an unknown/typo'd key instead of silently dropping it;
  - ``_estate_block`` / ``_reserve_cfg``: absence-testing block readers (DP#32);
  - ``_materialize_return_model_data`` / ``resolve_return_rate`` /
    ``set_return_rate``: the ONE place the investment return is read and written
    (DP#21);
  - ``has_readvanceable_facility`` / ``resolve_heloc_rate``: the ONE predicate
    and the ONE precedence order for the HELOC facility and its rate;
  - ``_dict_to_json``: the shared "dump a dict, optionally persist" serializer
    behind every ``to_json``.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from return_model import ReturnModel, ReturnEngine

logger = logging.getLogger(__name__)

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
# ONE loading boundary (``input_contract.validate_contract``, called from
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
    # Dated zero-emission vehicle acquisitions. Household-level, not under
    # 'family': the incentive is paid on the vehicle, not to a member.
    'zev_purchases',
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
            f"_INTERNAL_ROOT_ALLOWED_KEYS in config_access.py."
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
