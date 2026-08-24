#!/usr/bin/env python3
"""
Scenario Discovery Module (DP#16) — Pure-function trigger detection

Auto-discovers anchor scenarios from input.json data using trigger rules.
This is a pure function module with no side effects or I/O.

All scenarios are generated from configuration data, not hardcoded values.
Implements trigger-based discovery as specified in AGENTS.md.

Usage:
    from scenario_discovery import discover_anchors
    
    cfg = json.load(open('input.json'))
    anchors = discover_anchors(cfg)
    
    # Returns dict with keys: income, mortgage, refinance, strategy, 
    # resp_action, sm_options, deduct_later_options, child_accounts
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Optional, TYPE_CHECKING

from tax_data import TaxDataProvider
from jurisdiction_providers import get_provider
from member_config import find_member_by_role, projection_span

# DP#25 (issue #998): the scenario layer must not import from the simulation
# layer (tax_calculator / strategy / simulation_config). The simulation
# concepts this module needs at call time -- ``marginal_rate``, the strategy
# dataclasses/engine (``FamilyState`` / ``ChildState`` / ``StrategyEngine`` /
# ``AllocationStrategy``), and the rate resolvers (``resolve_return_rate`` /
# ``resolve_heloc_rate``) -- are INJECTED by the simulation/optimization caller
# through ``SimulationDeps`` (see ``configure_simulation_deps`` below) rather
# than imported here. ``find_member_by_role`` / ``projection_span`` are pure
# config-reading helpers relocated to the data layer (``member_config``).
#
# The strategy types are imported only under TYPE_CHECKING so the annotations
# on ``_build_family_state`` / ``_sweep_child_allocation`` stay accurate
# without creating a runtime dependency on the simulation layer.
if TYPE_CHECKING:
    from strategy import FamilyState, ChildState


@dataclass
class SimulationDeps:
    """The simulation-layer callables/classes ``scenario_discovery`` needs at
    call time, injected by the simulation/optimization caller (DP#25, #998).

    The scenario layer declares WHAT it needs (this bundle); the simulation
    layer provides the concrete callables (``simulation_deps.build_simulation_deps``).
    This is dependency inversion: the scenario layer never imports
    ``tax_calculator`` / ``strategy`` / ``simulation_config`` at runtime; it
    receives them through this bundle, populated once at simulation-layer
    import time via ``configure_simulation_deps``.
    """
    marginal_rate: Any
    FamilyState: Any
    ChildState: Any
    StrategyEngine: Any
    AllocationStrategy: Any
    resolve_return_rate: Any
    resolve_heloc_rate: Any


# The module-level injection point. ``None`` until the simulation layer
# configures it; ``discover_anchors`` resolves it lazily and fails loudly if it
# was never configured (DP#32 -- absence must fail, never silently no-op).
_SIM_DEPS: Optional[SimulationDeps] = None


def configure_simulation_deps(deps: SimulationDeps) -> None:
    """Inject the simulation-layer callables this module needs (DP#25, #998).

    Called once at import time by the simulation/optimization entry points
    (``simulate.py`` / ``optimize.py``) with ``build_simulation_deps()``. After
    this call, ``discover_anchors`` and the other public functions resolve the
    injected ``marginal_rate`` / strategy types / rate resolvers through
    ``_resolve_deps()`` instead of importing them at module top.
    """
    global _SIM_DEPS
    _SIM_DEPS = deps


def _resolve_deps(sim_deps: Optional[SimulationDeps] = None) -> SimulationDeps:
    """Return the explicitly-passed deps, or the module-level configured ones.

    DP#32: if neither is set, fail loudly -- a caller that reaches
    ``discover_anchors`` without the simulation layer having configured its
    injection point is a wiring bug, not a silent no-op.
    """
    deps = sim_deps if sim_deps is not None else _SIM_DEPS
    if deps is None:
        raise RuntimeError(
            "scenario_discovery: simulation dependencies were never injected. "
            "DP#25 (#998): the scenario layer no longer imports "
            "tax_calculator / strategy / simulation_config at runtime; a "
            "simulation/optimization-layer caller must call "
            "configure_simulation_deps(build_simulation_deps()) first (the "
            "entry points simulate.py / optimize.py do this at import time). "
            "A test that imports scenario_discovery in isolation must import "
            "simulation_deps (or simulate/optimize) first."
        )
    return deps

# Issue #303: default set of retirement ages to enumerate when the projection
# horizon actually reaches retirement (or enumeration is explicitly forced).
DEFAULT_RETIREMENT_CANDIDATE_AGES = [60, 62, 65, 67, 70]

# DP#25: Access jurisdiction providers through registry
STRATEGIES = get_provider('strategies')
try:
    from countries.canada.strategies import discover_strategies
except ImportError:
    discover_strategies = None


def _get_tax_brackets(cfg: dict, deps: Optional[SimulationDeps] = None):
    """Get combined tax brackets from config. Returns (brackets, primary_mtr, spouse_mtr)."""
    deps = _resolve_deps(deps)
    family = cfg.get('family', {})
    members = family.get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    spouse = find_member_by_role(members, 'spouse', {})
    primary_income = primary.get('gross_income', 0)
    spouse_income = spouse.get('gross_income', 0)
    brackets = TaxDataProvider().get_combined_brackets()
    primary_mtr = deps.marginal_rate(primary_income, brackets) if primary_income > 0 else 0
    spouse_mtr = deps.marginal_rate(spouse_income, brackets) if spouse_income > 0 else 0
    return brackets, primary_mtr, spouse_mtr


def discover_anchors(cfg: dict, force_retirement_ages: bool = False,
                      sim_deps: Optional[SimulationDeps] = None) -> dict:
    """Auto-discover anchor scenarios from input.json trigger data (DP#16).

    Returns dict with keys:
        income: List[dict]          # Each with keys: id, label, primary_income, spouse_income
        mortgage: List[dict]        # Each with keys: id, label, rate, type, term_years
        refinance: List[dict]       # Each with keys: id, label, cash_out, ltv
        strategy: List[dict]        # Each with keys: id, label, + allocation pcts + flags
        resp_action: List[str]      # ['keep', 'eap', 'collapse'] when resp > 0, else ['keep']
        sm_options: List[bool]      # [True, False] if conditions hold, else [False]
        deduct_later_options: List[bool]  # [True, False] if bracket_gap > 0, else [False]
        child_accounts: List[dict]  # Each with keys: child_name, fhsa_room, tfsa_room, rrsp_room
        retirement_age: List[int]   # Issue #303: candidate retirement ages to enumerate
        drawdown_order: List[dict]  # Issue #618: candidate decumulation orders to enumerate.
                                     # Each with keys: id, label, order (list of source tokens).
        mortgage_structure: List[dict]  # Issue #687: candidate mortgage STRUCTURES (all-mortgage
                                     # vs. readvanceable vs. mortgage+revolving-line) against the
                                     # SAME registered charge. Each with keys: id, label,
                                     # revolving_share, readvanceable, revolving_rate,
                                     # revolving_rate_type. Issue #1075: a structure may instead
                                     # carry the 3-tranche form (key: tranches -- house /
                                     # deductible investment / line), which the optimizer
                                     # expands into one sweep point per split. A single
                                     # 'declared' identity entry
                                     # (revolving_share=None) when decisions.mortgage.
                                     # structure_options was never declared -- no sweep, the
                                     # engine runs the declared liabilities[] as-is.
        draw_fraction_options: List[float]  # Issue #735: candidate fractions of
                                     # margin_available to actually draw and invest at year 0
                                     # (e.g. [0.0, 0.25, 0.5, 1.0]) -- [0.0] (undrawn only, no
                                     # sweep) when the household has no revolving facility at
                                     # all (margin_available <= 0), since there is nothing to
                                     # trade off.

    Args:
        cfg: Input configuration dict.
        force_retirement_ages: When True, always enumerate the configured
            retirement-age candidates even if the projection horizon does not
            reach retirement (e.g. the CLI --retirement-ages flag). When False
            (default), the dimension is gated on the horizon actually reaching
            the primary member's retirement age so short runs are not multiplied
            (see _discover_retirement_ages).
        sim_deps: Optional[SimulationDeps] -- the injected simulation-layer
            callables (marginal_rate, strategy types/engine, rate resolvers).
            When None, the module-level configured deps are used (set by
            configure_simulation_deps at simulation-layer import time, #998).
    """
    deps = _resolve_deps(sim_deps)
    scenarios = cfg.get('scenarios', {})

    # Initialize result structure
    result = {
        'income': [],
        'mortgage': [],
        'refinance': [],
        'strategy': [],
        'resp_action': [],
        'sm_options': [],
        'deduct_later_options': [],
        'child_accounts': [],
        'retirement_age': [],
        'drawdown_order': [],
        'mortgage_structure': [],
        'draw_fraction_options': [],
        'deposit_products': [],
    }
    
    # Income discovery (override check)
    income_scenarios = scenarios.get('income', [])
    if income_scenarios:
        # Override: use provided income scenarios
        result['income'] = _convert_income_scenarios(income_scenarios)
    else:
        # Auto-generate from family.members
        result['income'] = _discover_income_scenarios(cfg)
    
    # Mortgage discovery (override check)
    mortgage_scenarios = scenarios.get('mortgage', [])
    if mortgage_scenarios:
        # DP#33 (#890): a declared candidate ANNOTATES the auto-discovered sweep,
        # it does not REPLACE it. #853 established this for the refinance LTV
        # curve; here it is extended to the mortgage dimension, so a household
        # that declares one renewal rate still sees every rate path
        # auto-discovery found, with its own option marked in situ. Matched by
        # the discrete ``id`` (a rate scenario is categorical, not a point on a
        # curve), so a declared option that is not already an auto rung is added
        # alongside the sweep rather than truncating it.
        result['mortgage'] = annotate_declared_over_sweep(
            _discover_mortgage_scenarios(cfg),
            _convert_mortgage_scenarios(mortgage_scenarios, cfg),
            key='id', ndigits=None,
        )
    else:
        # Auto-generate from property options
        result['mortgage'] = _discover_mortgage_scenarios(cfg)
    
    # Refinance discovery (override check)
    refinance_scenarios = scenarios.get('refinance', [])
    if refinance_scenarios:
        # Override: use provided refinance scenarios
        result['refinance'] = _convert_refinance_scenarios(refinance_scenarios, cfg)
    else:
        # Auto-generate LTV levels
        result['refinance'] = _discover_refinance_scenarios(cfg)
    
    # Strategy discovery (override check)
    strategy_scenarios = scenarios.get('strategy', [])
    if strategy_scenarios:
        # DP#33 (#890): a declared strategy ANNOTATES the auto-discovered sweep,
        # it does not REPLACE it -- the household still sees every strategy
        # DP#6 discovery found, with its own declared split marked in situ.
        # Matched by the discrete ``id`` (a contribution split is categorical),
        # so a declared strategy that is not already an auto candidate is added
        # alongside the sweep rather than collapsing it to one point.
        result['strategy'] = annotate_declared_over_sweep(
            _discover_strategy_scenarios(cfg, deps),
            _convert_strategy_scenarios(strategy_scenarios),
            key='id', ndigits=None,
        )
    else:
        # Auto-generate using discover_strategies
        result['strategy'] = _discover_strategy_scenarios(cfg, deps)

    # RESP action discovery (override check)
    resp_action_scenarios = scenarios.get('resp_action', [])
    if resp_action_scenarios:
        # DP#33 (#890): a declared resp_action ANNOTATES the auto-discovered set
        # (keep/eap/collapse), it does not REPLACE it -- declaring one action
        # must not hide the others from the sweep. resp_action candidates are
        # bare action tokens (consumed as strings by simulate.build_all_overlays),
        # so the union is routed through the same annotate primitive (DP#9) by
        # wrapping each token as an ``{'id': token}`` row and unwrapping the
        # union back to the ``List[str]`` shape the engine reads.
        auto = [{'id': a} for a in _discover_resp_actions(cfg)]
        declared = [{'id': item['id']} for item in resp_action_scenarios]
        unioned = annotate_declared_over_sweep(auto, declared, key='id', ndigits=None)
        result['resp_action'] = [row['id'] for row in unioned]
    else:
        # Auto-generate based on resp balance
        result['resp_action'] = _discover_resp_actions(cfg)
    
    # Readvanceable mortgage strategy options discovery (trigger-based)
    result['sm_options'] = _discover_readvanceable_options(cfg, deps)

    # Deduct-later options discovery (trigger-based)
    result['deduct_later_options'] = _discover_deduct_later_options(cfg, deps)

    # Child accounts discovery (trigger-based)
    result['child_accounts'] = _discover_child_accounts(cfg, deps)

    # Retirement-age discovery (Issue #303, horizon-gated unless forced)
    result['retirement_age'] = _discover_retirement_ages(cfg, force=force_retirement_ages)

    # Drawdown-order discovery (Issue #618, horizon-gated the same way as
    # retirement_age -- decumulation order is a no-op fixed input on a
    # horizon that never reaches retirement).
    result['drawdown_order'] = discover_drawdown_orders(cfg)

    # Mortgage-structure discovery (Issue #687, trigger-based -- DP#16: the
    # household declaring decisions.mortgage.structure_options IS the
    # trigger; no CLI flag, no opt-in).
    result['mortgage_structure'] = _discover_structure_scenarios(cfg)

    # Draw-fraction discovery (Issue #735, trigger-based like sm_options
    # above: a declared revolving facility with room IS the trigger).
    result['draw_fraction_options'] = _discover_draw_fraction_options(cfg)

    # Deposit-product discovery (Issue #936, trigger-based -- DP#16: the
    # household declaring decisions.deposit_products IS the trigger; no CLI flag,
    # no opt-in). Absent => [] (no product swept), so enumerate_overlays runs the
    # single "leave it" baseline only, byte-identical to today (DP#32).
    result['deposit_products'] = _discover_deposit_products(cfg)

    return result


def _candidate_retirement_ages(cfg: dict) -> List[int]:
    """Return the configured retirement-age candidate set (Issue #303).

    Source of truth is cfg['retirement']['candidate_ages']; falls back to
    DEFAULT_RETIREMENT_CANDIDATE_AGES. Values are de-duplicated and sorted so
    the enumeration order is deterministic.
    """
    retirement = cfg.get('retirement', {}) or {}
    ages = retirement.get('candidate_ages')
    # DP#32 (#606): an explicit candidate_ages=[] ("do not sweep retirement
    # age") is a value, not absence -- only a genuinely missing key falls
    # back to the default sweep.
    ages = DEFAULT_RETIREMENT_CANDIDATE_AGES if ages is None else ages
    return sorted({int(a) for a in ages})


def _horizon_reaches_retirement(cfg: dict, candidate_ages: List[int]) -> bool:
    """True when the projection horizon reaches at least one candidate age.

    Issue #303 gating: enumerating retirement ages only changes the simulation
    if a member actually crosses one of the candidate ages within the
    projection window. We compute the primary member's age at the end of the
    horizon (start_year + projection_years) from birth_year and compare it
    against the smallest candidate. If the primary has no birth_year, the
    transition cannot be dated, so the dimension stays off (matching the
    pre-#294 accumulation model).

    Issue #618: the horizon span honors ``assumptions.horizon_age`` (DP#1 --
    "plan until the primary turns N", the source of truth SimulationConfig
    itself resolves via ``projection_span``), not just an explicit
    ``assumptions.projection_years`` count -- a long-horizon household
    configured with ``horizon_age`` (e.g. the #581 golden household) would
    otherwise silently gate OFF every horizon-gated dimension.
    """
    if not candidate_ages:
        return False
    # DP#25 (#998): projection_span is a data-layer helper (member_config),
    # imported at module top -- no simulation_config import here.
    assumptions = cfg.get('assumptions', {}) or {}
    start_year = assumptions.get('start_year', 2026)
    members = cfg.get('family', {}).get('members', [])
    projection_years = projection_span(
        horizon_age=assumptions.get('horizon_age'),
        start_year=start_year,
        members=members,
        projection_years=assumptions.get('projection_years', 0),
    )
    end_year = start_year + projection_years

    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    birth_year = primary.get('birth_year', 0)
    if not birth_year:
        return False
    age_at_horizon_end = end_year - birth_year
    return age_at_horizon_end >= min(candidate_ages)


def _discover_retirement_ages(cfg: dict, force: bool = False) -> List[int]:
    """Discover the retirement-age dimension (Issue #303).

    Returns the configured candidate ages when either the projection horizon
    reaches retirement OR enumeration is explicitly forced; otherwise returns a
    single-element list with the primary member's current retirement_age so the
    default short-horizon run is NOT multiplied by the candidate set (gating).
    """
    candidate_ages = _candidate_retirement_ages(cfg)
    if force or _horizon_reaches_retirement(cfg, candidate_ages):
        return candidate_ages

    # Gated off: keep the single baseline age so callers iterate exactly once.
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    return [primary.get('retirement_age', 65)]


def discover_drawdown_orders(cfg: dict) -> List[dict]:
    """Discover the decumulation-order dimension (Issue #618).

    `retirement.drawdown_order` was a fixed config input the engine read but
    the optimizer never varied -- so a strategy search could rank three
    accumulation strategies that differ by less than 1pp of death tax while
    missing a $500k+ swing that lives entirely in withdrawal ORDER (TFSA-first
    vs RRSP-meltdown vs bracket-filling), not account allocation.

    Gated exactly like _discover_retirement_ages (Issue #303): decumulation
    order is a no-op on a horizon that never reaches retirement, so a
    short-horizon accumulation-only run (e.g. a 10-year refinance decision)
    stays a single-candidate dimension and is NOT multiplied.

    Returns DRAWDOWN_ORDER_CANDIDATES (countries.canada.retirement_transition)
    when the horizon reaches the primary member's retirement age; otherwise a
    single-element list carrying whatever order the config already specifies
    (or the engine's own default), so callers that don't gate on this
    dimension see byte-identical behavior to before #618.
    """
    from countries.canada.retirement_transition import DRAWDOWN_ORDER_CANDIDATES

    retirement = cfg.get('retirement', {}) or {}
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    retirement_age = primary.get('retirement_age', 65)

    if _horizon_reaches_retirement(cfg, [retirement_age]):
        return DRAWDOWN_ORDER_CANDIDATES

    # Gated off: keep the single baseline order so callers iterate exactly
    # once. DP#13/#32: an explicit config order wins -- including an unusual
    # empty list -- absent that (key missing / None), mirror the engine's own
    # fallback (simulation.py) rather than opining here.
    baseline_order = retirement.get('drawdown_order')
    if baseline_order is None:
        baseline_order = ['tfsa', 'non_reg', 'rrsp']
    return [{'id': 'configured', 'label': 'Configured drawdown order', 'order': baseline_order}]


class IncomeScenarioError(ValueError):
    """Raised when ``scenarios.income[].members[]`` is missing a field the
    engine must have in order to price the override correctly.

    Issue #674: the fields (``kind``, ``gross_income``, ``from``, ``to``) are
    all schema-``required`` on ``$defs/income_override``, so a document that
    came through ``input_contract.load_and_map`` -- the ONE loading boundary
    -- can never reach here short. This is the guard for the other way in:
    an internal config assembled in code (a test fixture, a script) that
    predates the dated/kind-classified shape. It fails by name rather than
    with a bare ``KeyError``, so the caller is told which scenario, which
    role, and which field.
    """


def _require(member: dict, field: str, scenario: dict, nullable: bool = False):
    """Read a required field off an income-override member, or refuse.

    Deliberately NOT ``member.get(field, <default>)``. There is no default
    for any of these fields, and specifically none for ``kind``: see
    ``_convert_income_scenarios``' docstring and DP#32.
    """
    if field not in member:
        raise IncomeScenarioError(
            f"scenarios.income[id={scenario.get('id', 'unknown')!r}]"
            f".members[role={member.get('role', 'unknown')!r}] is missing "
            f"required field {field!r}. Issue #674: an income override must "
            f"declare its kind (employment / ei / self_employment / ...) and "
            f"its [from, to) window explicitly -- `to: null` is the explicit "
            f"spelling of 'permanent'. There is no default: an undeclared "
            f"kind assumed to be employment would accrue RRSP contribution "
            f"room that ITA s.146(1) does not grant to, e.g., EI benefits "
            f"(DP#32 -- absence must fail loudly, never default)."
        )
    value = member[field]
    if value is None and not nullable:
        raise IncomeScenarioError(
            f"scenarios.income[id={scenario.get('id', 'unknown')!r}]"
            f".members[role={member.get('role', 'unknown')!r}] has "
            f"{field!r} = None. Only 'to' may be null (meaning 'permanent')."
        )
    return value


def _convert_income_scenarios(income_scenarios: List[dict]) -> List[dict]:
    """Convert scenarios.income format to standard format.

    issue #665: a scenario that overrides only ONE earner (the realistic
    "primary loses their job" case) must leave the OTHER earner's income
    untouched -- ``primary_income``/``spouse_income`` are ``None`` when the
    scenario's ``members`` list has no entry for that role, never coerced to
    0 (DP#32). Every consumer of this list already expects that convention
    (``scenario_overlay.apply_overlay``: "if overlay.spouse_income is not
    None"; ``simulate.py``'s own comments: "None if not set -> keeps base").
    Defaulting to 0 here would silently zero out the unmentioned earner's
    income the moment a household declares a partial override -- exactly the
    kind of confident-but-wrong number DP#32 exists to prevent.

    Issue #674: a role's entries are also collected into
    ``primary_segments``/``spouse_segments`` -- a LIST, because one scenario
    may declare more than one dated override for the same role (a job-loss
    window followed by a differently-priced re-employment window is two
    ``income_override`` entries with the same ``income_id`` and adjoining
    ``from``/``to`` windows, issue #674's "small schedule of segments").
    Each entry carries ``kind``/``amount``/``from``/``to`` -- everything
    ``simulation.py``'s ``_income_components_for_year`` needs to compute the
    year-by-year blend of segment income vs. the base (pre-scenario) income,
    and everything ``simulation_rules.py``'s RRSP-room accrual needs to
    exclude a non-earned kind (ITA s.146(1); ``EARNED_INCOME_KINDS``).
    ``primary_income``/``spouse_income`` are kept for existing DISPLAY
    consumers (e.g. the CLI report's "income scenario" column) -- the
    EARLIEST segment's amount, or ``None`` when the role has no segments.
    Engine logic must read ``*_segments``, never these display scalars, or
    it is back to one flat number for the whole horizon (#674's Gap 1).

    A scenario built by ``_discover_income_scenarios`` (the always-present
    auto-discovered "current income" baseline) has no ``members`` key at
    all -- it produces empty segment lists, i.e. "no override", exactly
    like an explicit empty ``overrides: []`` scenario.

    An override member that omits any of ``kind``/``gross_income``/``from``/
    ``to`` is REFUSED here, by name (``IncomeScenarioError``), and there is
    no default for any of them -- least of all ``kind``. The tempting
    one-liner is ``member.get('kind', 'employment')``, and it is the precise
    bug #674 exists to fix: an undeclared kind would silently become earned
    income, silently accrue 18% RRSP room that ITA s.146(1) does not grant
    (EI benefits are not "earned income"), and print a confident wrong
    refund. Absence is not employment; absence is an error (DP#32, DP#13).
    """
    result = []
    for scenario in income_scenarios:
        primary_segments: List[dict] = []
        spouse_segments: List[dict] = []

        for member in scenario.get('members', []):
            segment = {
                'kind': _require(member, 'kind', scenario),
                'amount': _require(member, 'gross_income', scenario),
                'from': _require(member, 'from', scenario),
                # `to` is required but NULLABLE: null is the explicit
                # spelling of "permanent" (see $defs/income_override.to),
                # so absence of the KEY is the error, not a None value.
                'to': _require(member, 'to', scenario, nullable=True),
                # Issue #980 (T2125): the override's optional professional-
                # expense total, carried onto the dated segment so
                # simulation.py's _self_employment_net_amount can derive
                # NET business income (gross - expenses) for a self_employment
                # segment. None when the override declares no expenses (DP#32:
                # the engine taxes the full gross, byte-identical to pre-#980).
                # Read with .get, NOT _require -- expenses_annual is OPTIONAL.
                'expenses_annual': member.get('expenses_annual'),
            }
            if member.get('role') == 'primary':
                primary_segments.append(segment)
            elif member.get('role') == 'spouse':
                spouse_segments.append(segment)

        def _display_amount(segments: List[dict]) -> Optional[float]:
            if not segments:
                return None
            return min(segments, key=lambda s: s['from'])['amount']

        result.append({
            'id': scenario.get('id', 'unknown'),
            'label': scenario.get('label', 'Income scenario'),
            'primary_income': _display_amount(primary_segments),
            'spouse_income': _display_amount(spouse_segments),
            'primary_segments': primary_segments,
            'spouse_segments': spouse_segments,
        })

    return result


def _discover_income_scenarios(cfg: dict) -> List[dict]:
    """Auto-generate income scenarios from family.members.

    Issue #674: emits ``primary_segments``/``spouse_segments`` explicitly as
    EMPTY LISTS. The auto-discovered baseline is the household's income as
    declared -- it overrides nothing, so it has no dated segments, and `[]`
    is the honest statement of that. Emitting them makes the income-anchor
    shape TOTAL: every entry of ``discover_anchors(cfg)['income']``, from
    either producer, carries both keys. ``optimize._apply_income_scenario``
    can then read them by subscript, so a producer that ever forgets them
    crashes instead of being silently read as "this scenario overrides
    nobody" -- which is how a declared override evaporates unnoticed
    (#665's "parsed, mapped, then never passed"; DP#32).
    """
    family = cfg.get('family', {})
    members = family.get('members', [])
    
    primary_income = 0
    spouse_income = 0
    
    for member in members:
        role = member.get('role', '')
        income = member.get('gross_income', 0)
        
        if role == 'primary':
            primary_income = income
        elif role == 'spouse':
            spouse_income = income
    
    return [{
        'id': 'current',
        'label': 'Current income',
        'primary_income': primary_income,
        'spouse_income': spouse_income,
        'primary_segments': [],
        'spouse_segments': [],
    }]


def _convert_mortgage_scenarios(mortgage_scenarios, cfg) -> List[dict]:
    """Convert scenarios.mortgage format to standard format."""
    property_cfg = cfg.get('property', {})
    current_rate = property_cfg.get('mortgage_rate', 0.05)

    result = []
    for scenario in mortgage_scenarios:
        rate = scenario.get('rate')
        if rate is None:
            if scenario.get('rate_path') == 'current':
                rate = current_rate
            else:
                rate = current_rate  # fallback
        result.append({
            'id': scenario.get('id', 'unknown'),
            'label': scenario.get('label', 'Mortgage scenario'),
            'rate': rate,
            'type': scenario.get('type', 'variable'),
            'term_years': scenario.get('term_years', 1)
        })
    return result


def _discover_mortgage_scenarios(cfg: dict) -> List[dict]:
    """Auto-generate mortgage scenarios from property options."""
    property_cfg = cfg.get('property', {})
    result = []
    
    # Get refinance options
    refinance_options = property_cfg.get('refinance_options', [])
    for option in refinance_options:
        result.append({
            'id': f"refi_{option.get('name', 'option').lower().replace(' ', '_')}",
            'label': f"Refinance: {option.get('name', 'Unknown option')}",
            'rate': option.get('rate', 0.05),
            'type': option.get('type', 'variable'),
            'term_years': option.get('term_years', 5)
        })
    
    # Get renewal options
    renewal_options = property_cfg.get('renewal_options', [])
    for option in renewal_options:
        result.append({
            'id': f"renew_{option.get('name', 'option').lower().replace(' ', '_')}",
            'label': f"Renewal: {option.get('name', 'Unknown option')}",
            'rate': option.get('rate', 0.05),
            'type': option.get('type', 'variable'),
            'term_years': option.get('term_years', 5)
        })
    
    # If no options, create one from current rate
    if not result:
        current_rate = property_cfg.get('mortgage_rate', 0.05)
        result.append({
            'id': 'current',
            'label': 'Current mortgage rate',
            'rate': current_rate,
            'type': 'variable',
            'term_years': 1
        })
    
    return result


def _discover_structure_scenarios(cfg: dict) -> List[dict]:
    """Issue #687: candidate mortgage STRUCTURES against the SAME
    registered charge -- all-mortgage vs. readvanceable vs. mortgage +
    undrawn line -- read from ``property.structure_options`` (mapped by
    ``input_contract.py`` from ``decisions.mortgage.structure_options``).

    DP#16: the household DECLARING structure_options is the trigger; there
    is no CLI flag, no opt-in. Absent (the household has not declared this
    as an open decision), this returns a single identity/baseline entry
    (``revolving_share: None``) so callers that cross this dimension with
    every other (income, retirement age, ...) still iterate exactly once,
    applying no structural change at all (``property_structure.
    apply_structure_overlay`` treats ``revolving_share is None`` as a no-op
    -- DP#13/DP#32: absence is not an opinion, it is "run the declared
    liabilities[] as-is").
    """
    structure_options = cfg.get('property', {}).get('structure_options', [])
    if not structure_options:
        return [{
            'id': 'declared', 'label': 'Declared structure (no structure_options swept)',
            'revolving_share': None, 'readvanceable': None,
            'revolving_rate': None, 'revolving_rate_type': None,
        }]
    return [dict(opt) for opt in structure_options]


def _discover_deposit_products(cfg: dict) -> List[dict]:
    """Issue #936: the declared deposit products (a plain HISA, a term/GIC, a
    promotional teaser -- one generic mechanism) the optimizer sweeps
    take-vs-leave, read from ``deposit_products`` (mapped by
    ``input_contract.py`` from ``decisions.deposit_products``).

    DP#16: the household DECLARING ``decisions.deposit_products`` is the trigger;
    there is no CLI flag, no opt-in. UNLIKE ``_discover_structure_scenarios``
    (whose absent state is a single identity/baseline entry that still iterates
    once), the "leave it" baseline for a deposit product is the ABSENCE of any
    product -- ``enumerate_overlays`` always emits the no-product baseline
    overlay and then adds ONE take-it variant per declared product. So absence
    returns an EMPTY list here: no take-it variant is generated, only the
    baseline runs, byte-identical to today (DP#32 -- an absent product is not a
    $0 product). Each declared product passes through unchanged (it is already
    the shape ``apply_overlay`` / ``SimState.initial`` read).
    """
    return [dict(product) for product in cfg.get('deposit_products', [])]


def discover_property_funding_cells(cfg: dict) -> List[dict]:
    """Issue #1011: the candidate FUNDING assignments the optimizer sweeps and
    ranks for the household's dated property PURCHASES.

    A property whose ``purchase`` declares ``funding_options`` (mapped by
    ``input_contract.py`` onto ``cfg['properties'][N]['purchase']
    ['funding_options']``) asks the optimizer to RANK the ways to fund it --
    all-cash from the portfolio vs. a down-payment + originated mortgage at a
    declared LTV -- rather than funding it one fixed way. This returns one
    CELL per funding assignment: a ``{property_id: option}`` mapping saying
    which option each declaring property is funded by in that cell.

    When the household owns SEVERAL properties that each declare
    ``funding_options`` (e.g. a rental bought in 2030 and a cottage bought in
    2032), the funding choice is INDEPENDENT per property, so the sweep is the
    CROSS PRODUCT of the declaring properties' option lists -- a cell funds
    every such property at once. The common case (one purchase with options)
    is just that property's list.

    DP#16: the household DECLARING ``purchase.funding_options`` is the
    trigger; there is no CLI flag, no opt-in. ABSENT (no property declares any
    ``funding_options``), this returns an EMPTY list -- the exploration is
    not run at all and the household's purchase is funded the single fixed way
    its ``financing`` declares (or equity-financed), byte-identical to
    #967/#696 (DP#32 -- an absent sweep is not a one-option sweep).

    Pure: reads ``cfg['properties']``, builds cells, runs no simulation. Each
    cell carries an ``id``/``label`` (joined from the chosen options) so the
    exploration can tag every ranked result row with the funding it was scored
    at (DP#9: the basis travels ON the row).
    """
    import itertools

    declaring: List[tuple] = []  # (property_id, options)
    for prop in cfg.get('properties', []):
        purchase = prop.get('purchase')
        if purchase is None:
            continue
        options = purchase.get('funding_options')
        if not options:
            continue
        declaring.append((prop['id'], options))

    if not declaring:
        return []

    cells: List[dict] = []
    for combo in itertools.product(*[opts for _, opts in declaring]):
        assignment = {pid: dict(opt) for (pid, _), opt in zip(declaring, combo)}
        ids = '+'.join(opt['id'] for opt in combo)
        labels = ' | '.join(opt['label'] for opt in combo)
        cells.append({'id': ids, 'label': labels, 'assignment': assignment})
    return cells


def _convert_refinance_scenarios(refinance_scenarios, cfg) -> List[dict]:
    """Convert scenarios.refinance format to standard format.

    Issue #846: ``ltv`` is DERIVED whenever the scenario does not state one --
    including for a ``cash_out`` of 0. This used to read
    ``if ltv == 0 and cash_out > 0``, so a declared no-cash-out option (#846's
    "take the surplus from the line, not a mortgage advance" -- the exact option
    the issue opens with) was labelled **0% LTV** rather than the household's
    real current LTV. A household carrying a $340k mortgage on a $650k house is
    at 52%, not 0%, when it declines to refinance; ``_discover_refinance_
    scenarios`` has always labelled its own 'no_refinance' rung correctly, and
    the two producers must not disagree about the same rung (DP#9). Harmless
    while only simulate.py's overlay LABELS consumed this; #846 puts these rows
    in optimize.py's LTV EXPLORATION table, where a wrong 0% would misstate the
    very basis #845 exists to have stated.

    DP#32: absence (``ltv`` key missing) is what triggers derivation -- a
    scenario that STATES its ltv keeps it, including a genuine 0.
    """
    property_cfg = cfg.get('property', {})
    house_value = property_cfg.get('house_value', 0)
    mortgage_balance = property_cfg.get('mortgage_balance', 0)

    result = []
    for scenario in refinance_scenarios:
        cash_out = scenario.get('cash_out', 0)
        declared_ltv = scenario.get('ltv')
        if declared_ltv is not None:
            ltv = declared_ltv
        elif house_value > 0:
            ltv = round((mortgage_balance + cash_out) / house_value, 4)
        else:
            # No house value: an LTV is not computable, and inventing one would
            # be exactly the confident-wrong-number this repo exists to prevent.
            ltv = 0
        result.append({
            'id': scenario.get('id', 'unknown'),
            'label': scenario.get('label', 'Refinance scenario'),
            'cash_out': cash_out,
            'ltv': ltv
        })
    return result


def _discover_refinance_scenarios(cfg: dict) -> List[dict]:
    """Auto-generate refinance scenarios from LTV levels (continuous exploration).
    
    Explores LTV range from current mortgage LTV to ltv_max in 5% steps.
    This allows finding optimal cash-out between 0% and 80% maximum.
    """
    property_cfg = cfg.get('property', {})
    house_value = property_cfg.get('house_value', 0)
    mortgage_balance = property_cfg.get('mortgage_balance', 0)
    margin_available = property_cfg.get('margin_available', 0)
    ltv_max = property_cfg.get('ltv_max', 0.80)
    
    if house_value <= 0 or margin_available <= 0:
        return []
    
    result = []
    
    # Current LTV (no refinance with cash out)
    current_ltv = mortgage_balance / house_value if house_value > 0 else 0
    
    # Include "no refinance" option (cash_out = 0)
    result.append({
        'id': 'no_refinance',
        'label': 'Renew current mortgage only (no cash-out)',
        'cash_out': 0,
        'ltv': round(current_ltv, 4)
    })
    
    # Generate loan-to-value percentages from 40% to ceil(ltv_max) in 5% steps
    # This allows optimizer to find the optimal point
    end_pct = int(ltv_max * 100)
    
    for pct in range(40, end_pct + 1, 5):
        ltv = pct / 100.0
        cash_out = round(house_value * ltv - mortgage_balance, -3)
        if cash_out < 0:
            cash_out = 0
            
        result.append({
            'id': f'ltv_{pct}pct',
            'label': f'Refinance to {pct}% LTV',
            'cash_out': round(cash_out, -3),
            'ltv': ltv
        })
    
    return result


# ── Issue #846: declared candidates must never SILENTLY collapse a sweep ────

# #846 identified FOUR dimensions `discover_anchors` resolved with the
# declared-REPLACES-auto pattern: declaring ONE candidate reduced the dimension
# to a single point while every downstream surface kept printing like an
# exploration. #853 (refinance, in optimize.py's LTV table) and #890
# (mortgage/strategy/resp_action, in `discover_anchors` itself) since converted
# those to the DP#33 ANNOTATE pattern -- a declared candidate now UNIONS over the
# auto-discovered sweep rather than replacing it, so it can no longer narrow.
#
# The one surface where a declaration STILL replaces the sweep is `refinance` in
# simulate.py's grid (`discover_anchors['refinance']` is unchanged; #853 unioned
# only optimize.py's separate LTV table). So `refinance` is the sole remaining
# narrowable dimension, and the loud notice below exists for it alone. Measured
# on `schema/example.json`: declaring the two `decisions.mortgage.refinance_
# options` collapses simulate.py's grid from 240 overlays to 48 -- a 5x
# narrowing, with nothing said.
#
# DP#32: this is the same guarantee #771 already enforces for
# `sensitivity.sweeps` ("a mistyped path must never silently produce a
# single-point run that looks like a sweep"). A household is entitled to
# override -- an override is a legitimate, declared decision -- but it is NOT
# entitled to have the override pass unremarked.
NARROWABLE_DIMENSIONS = ('refinance',)

# The contract path each narrowable dimension is declared at, so the notice
# names the key the household actually typed rather than the internal
# `scenarios.*` alias input_contract.py maps it onto.
_DIMENSION_CONTRACT_PATHS = {
    'refinance': 'decisions.mortgage.refinance_options',
}


def _auto_candidates(cfg: dict, dimension: str) -> List:
    """The candidate list auto-discovery WOULD have produced for ``dimension``.

    DP#9: dispatches to the very function ``discover_anchors`` calls on the
    else-branch, so the counted comparison can never drift from the branch it
    describes.
    """
    if dimension == 'refinance':
        return _discover_refinance_scenarios(cfg)
    raise ValueError(
        f"{dimension!r} is not a narrowable dimension {NARROWABLE_DIMENSIONS}. "
        f"Issue #846: this helper exists to compare a DECLARED candidate list "
        f"against the auto ladder it REPLACES; the mortgage/strategy/resp_action "
        f"dimensions now ANNOTATE their sweep instead (#890) and so cannot narrow."
    )


def discover_narrowings(cfg: dict) -> List[dict]:
    """Report which declared candidate lists NARROW their auto-discovered sweep.

    Pure function (DP#16): computes counts only, runs no simulation. This is the
    logic half of the loud-narrowing notice; ``format_narrowings`` below renders
    it, and both CLIs print the same rendering (DP#9).

    Returns one dict per DECLARED narrowable dimension (a dimension the household
    never declared cannot narrow anything, so it is absent). After #890 the sole
    narrowable dimension is ``refinance`` (in simulate.py's grid); the other three
    now ANNOTATE their sweep and cannot narrow, so they never appear here. Each
    row carries:

        dimension, contract_path, declared_count, auto_count,
        collapsed_to_single_point, fewer_than_auto, narrowed

    ``narrowed`` is True when the declared list is a single point OR is shorter
    than the auto ladder. Both counts are always reported, so the notice states a
    fact rather than an opinion: a household that declared 2 candidates against
    an auto ladder of 10 has narrowed the sweep 5x even though 2 > 1.
    """
    declared_scenarios = cfg.get('scenarios', {})
    narrowings = []
    for dimension in NARROWABLE_DIMENSIONS:
        declared = declared_scenarios.get(dimension, [])
        if not declared:
            # DP#32: absence is not a narrowing -- auto-discovery ran, in full.
            continue
        declared_count = len(declared)
        auto_count = len(_auto_candidates(cfg, dimension))
        collapsed = declared_count == 1
        fewer = declared_count < auto_count
        narrowings.append({
            'dimension': dimension,
            'contract_path': _DIMENSION_CONTRACT_PATHS[dimension],
            'declared_count': declared_count,
            'auto_count': auto_count,
            'collapsed_to_single_point': collapsed,
            'fewer_than_auto': fewer,
            'narrowed': collapsed or fewer,
        })
    return narrowings


def format_narrowings(narrowings: List[dict]) -> List[str]:
    """Render ``discover_narrowings``' rows as console lines (DP#9: ONE
    spelling, so optimize.py and simulate.py say the same thing).

    Returns ``[]`` when nothing was narrowed -- a household whose declarations
    widened, or matched, the auto ladder is told nothing, so the notice stays a
    signal rather than boilerplate that gets skimmed past.
    """
    narrowed = [n for n in narrowings if n['narrowed']]
    if not narrowed:
        return []

    lines = [
        "",
        "=" * 120,
        "  ⚠️  DECLARED CANDIDATES REPLACED THE AUTO-DISCOVERED EXPLORATION (issue #846)",
        "=" * 120,
        "  Your declaration WINS -- that is by design. But it REPLACED the ladder this tool would",
        "  otherwise have swept, so the dimensions below were explored at fewer points than the",
        "  rankings may suggest. A single-point dimension is NOT an exploration of it:",
        "",
    ]
    for n in narrowed:
        point = " ← SINGLE POINT, not a sweep" if n['collapsed_to_single_point'] else ""
        lines.append(
            f"    • {n['dimension']:<12} you declared {n['declared_count']} candidate(s); "
            f"auto-discovery would have swept {n['auto_count']}{point}"
        )
        lines.append(f"      declared at: {n['contract_path']}")
    lines.append("")
    lines.append("  To restore the full sweep for a dimension, remove your declared candidates for it.")
    lines.append("")
    return lines


# ── Issue #853 / DP#33: a declaration is a LENS, not a BLINDFOLD ─────────────
#
# #846 made a declared candidate list REPLACE the auto-discovered sweep for its
# dimension (and #848 made that replacement loud). Honest, but it forces a bad
# trade: to ask "how do MY two options compare?", the household loses the whole
# curve that gives those two numbers their meaning -- and never learns that a
# rung they did NOT declare might beat both. DP#33: a declaration should ANNOTATE
# the swept exploration, not truncate it. Run the full sweep AND mark where each
# declared candidate falls within it (marking a coincident rung, or inserting the
# declared point in situ when it lands between rungs), so the ranking still
# contains every rung the household did not think to ask about.


def _annotation_fields(declared: bool = False,
                       declared_id: Optional[str] = None,
                       declared_label: Optional[str] = None) -> dict:
    """The DP#33 provenance keys every unioned candidate carries (issue #853).

    Emitted on EVERY row (both swept and inserted) so a consumer can subscript
    them unconditionally -- a producer that ever forgets them crashes rather than
    being silently read as "not a declared candidate" (DP#32, the #674 rule).
    """
    return {
        'declared': declared,
        'declared_id': declared_id,
        'declared_label': declared_label,
        'annotated': True,
    }


def annotate_declared_over_sweep(swept: List[dict], declared: List[dict],
                                 key: str, ndigits: Optional[int] = 4) -> List[dict]:
    """Union declared candidates OVER the auto-discovered sweep (DP#33, #853/#890).

    Returns the full ``swept`` exploration with each declared candidate made
    visible IN SITU rather than replacing the sweep:

    - A declared candidate whose ``key`` COINCIDES with a swept rung MARKS that
      rung -- the rung keeps its own fields but gains the declaration's
      ``id``/``label`` -- so the household sees "this rung is one of the options I
      asked about" without the sweep losing it.
    - A declared candidate that does NOT coincide with any swept rung is added as
      an extra row (carrying the declaration's own fields), so it is explored
      alongside the sweep rather than dropped.

    ``key`` is the field the two candidate sets are matched on. ``ndigits``
    selects HOW they are matched, so ONE primitive (DP#9) serves both shapes of
    dimension:

    - ``ndigits`` is an int (the #853 default, e.g. the refinance ``ltv`` curve):
      a CONTINUOUS key. Coincidence is compared at ``ndigits`` precision, a
      non-coincident declared candidate is INSERTED in situ, and the result is
      re-sorted by ``key`` so that in-situ point is ranked where it sits on the
      curve.
    - ``ndigits`` is ``None`` (a DISCRETE/categorical key, e.g. a strategy or
      RESP-action ``id``, #890): coincidence is exact-value. There is no
      "between" to sort into, so the swept order is PRESERVED and non-coincident
      declared candidates are APPENDED in declared order -- re-sorting a
      categorical dimension would impose an arbitrary order.

    Guarantees (the acceptance of #853/#890):
    - The result is NEVER shorter than ``swept`` -- declaring never reduces the
      explored set.
    - Every declared candidate is present and flagged ``declared=True`` with its
      id/label; every purely-swept rung is flagged ``declared=False``.
    - Pure function (DP#16): no I/O, no simulation, deterministic order.

    ``swept`` and ``declared`` are lists of candidate dicts that share the field
    ``key`` and carry ``id``/``label``. The inputs are not mutated; each returned
    row is a fresh dict.
    """
    numeric = ndigits is not None

    def coincidence_key(value):
        return round(value, ndigits) if numeric else value

    result: List[dict] = []
    # Index the swept rungs by their coincidence key so a declared candidate can
    # find the rung it coincides with in one pass.
    swept_by_key = {coincidence_key(s[key]): s for s in swept}
    declared_by_key = {}
    inserted: List[dict] = []
    for d in declared:
        dk = coincidence_key(d[key])
        if dk in swept_by_key:
            # Coincides with a rung -> mark that rung (last declaration wins if
            # two declared options map to the same rung; both are the same point).
            declared_by_key[dk] = d
        else:
            # No coincident rung -> add the declared candidate itself.
            row = dict(d)
            row.update(_annotation_fields(True, d.get('id'), d.get('label')))
            inserted.append(row)

    for s in swept:
        sk = coincidence_key(s[key])
        d = declared_by_key.get(sk)
        row = dict(s)
        if d is None:
            row.update(_annotation_fields(False))
        else:
            row.update(_annotation_fields(True, d.get('id'), d.get('label')))
        result.append(row)

    result.extend(inserted)
    # Continuous key: sort so an inserted between-rung candidate is ranked in
    # situ on the curve. Discrete key: preserve the swept order (see docstring).
    if numeric:
        result.sort(key=lambda r: r[key])
    return result


def _calculate_total_registered_room(cfg: dict) -> float:
    """Calculate total registered account room for the family."""
    total = 0
    
    # Family members
    family = cfg.get('family', {})
    members = family.get('members', [])
    for member in members:
        total += member.get('rrsp_room_accumulated', 0)
        total += member.get('tfsa_room_accumulated', 0)
        total += member.get('fhsa_room_accumulated', 0)
    
    # Children
    children = family.get('children', [])
    for child in children:
        total += child.get('fhsa_room_accumulated', 0)
        total += child.get('tfsa_room_accumulated', 0)
        total += child.get('rrsp_room_accumulated', 0)
    
    return total


def _convert_strategy_scenarios(strategy_scenarios: List[dict]) -> List[dict]:
    """Convert scenarios.strategy format to standard format."""
    result = []
    for scenario in strategy_scenarios:
        strategy_dict = {
            'id': scenario.get('id', 'unknown'),
            'label': scenario.get('label', 'Strategy'),
            'rrsp_pct': scenario.get('rrsp_pct', 0.0),
            'spousal_rrsp_pct': scenario.get('spousal_rrsp_pct', 0.0),
            'tfsa_pct': scenario.get('tfsa_pct', 0.0),
            'fhsa_pct': scenario.get('fhsa_pct', 0.0),
            'resp_pct': scenario.get('resp_pct', 0.0),
            'non_reg_pct': scenario.get('non_reg_pct', 0.0),
            'prioritize_readvanceable': scenario.get('use_smith', False),
            'deduct_later': scenario.get('deduct_later', False),
            'child_fhsa_pct': scenario.get('child_fhsa_pct', 0.0),
            'child_tfsa_pct': scenario.get('child_tfsa_pct', 0.0),
            'child_rrsp_pct': scenario.get('child_rrsp_pct', 0.0),
            'child_non_reg_pct': scenario.get('child_non_reg_pct', 0.0),
        }
        result.append(strategy_dict)
    return result


def _discover_strategy_scenarios(cfg: dict, deps: Optional[SimulationDeps] = None) -> List[dict]:
    """Auto-generate strategy scenarios using DP#6 discovery."""
    deps = _resolve_deps(deps)
    # Build FamilyState from cfg
    family_state = _build_family_state(cfg, deps)
    
    # Get mortgage config for discover_strategies
    property_cfg = cfg.get('property', {})
    mortgage_cfg = {
        'heloc_readvance': property_cfg.get('heloc_readvance', False)
    }
    
    # Get investment assumptions
    assumptions = cfg.get('assumptions', {})
    # DP#21: resolved from return_model, the single source of truth (issue
    # #591) -- assumptions.investment_return is a deprecated scalar that can
    # silently disagree with return_model once an overlay has been applied.
    investment_return = deps.resolve_return_rate(cfg)
    # Issue #677: this used to read assumptions.get('heloc_rate', 0.05) --
    # so a household with no FIXED rate_paths.heloc silently fell through to
    # a hardcoded 5%. Resolved via the canonical helper instead.
    # Issue #685 made the claim this comment already made actually TRUE: the
    # household's OWN declared property.heloc_rate wins. It did not, before
    # #685 -- input_contract.py piped the rate_paths BELIEF into
    # assumptions.heloc_rate, resolve_heloc_rate's tier 1, which outranks the
    # signed rate. The contract loader no longer writes that key; tier 1 is
    # now reserved for a deliberate anchor DECISION (apply_anchor_preset).
    # default=0.0 only matters when this household has no readvanceable
    # facility at all -- mortgage_cfg above then carries no HELOC
    # (heloc_readvance is only True for a real facility), so
    # is_readvanceable is False regardless of heloc_rate's value and
    # 'readvance_priority' can never be selected here (#663): the value is
    # computed and discarded, never surfaced.
    heloc_rate = deps.resolve_heloc_rate(cfg, default=0.0)
    capital_gains_inclusion = assumptions.get('capital_gains_inclusion', 0.5)
    
    # Discover applicable strategies
    strategies = discover_strategies(
        family_state, 
        mortgage_cfg=mortgage_cfg,
        investment_return=investment_return,
        heloc_rate=heloc_rate,
        capital_gains_inclusion=capital_gains_inclusion
    )
    
    # Convert to list of dicts
    result = []
    for strategy_name, strategy in strategies.items():
        strategy_dict = {
            'id': strategy_name,
            'label': strategy.name,
            'rrsp_pct': strategy.rrsp_pct,
            'spousal_rrsp_pct': strategy.spousal_rrsp_pct,
            'tfsa_pct': strategy.tfsa_pct,
            'fhsa_pct': strategy.fhsa_pct,
            'resp_pct': strategy.resp_pct,
            'non_reg_pct': strategy.non_reg_pct,
            'prioritize_readvanceable': strategy.prioritize_readvanceable,
            'deduct_later': strategy.deduct_later,
            # Issue #812 (#701 follow-up): these are now REAL AllocationStrategy
            # dimensions, no longer hardcoded 0.0 placeholders. They carry the
            # strategy's declared child-allocation targets (0.0 = the #701 room-
            # priority default in StrategyEngine.allocate_child).
            'child_tfsa_pct': strategy.child_tfsa_pct,
            'child_fhsa_pct': strategy.child_fhsa_pct,
            'child_rrsp_pct': strategy.child_rrsp_pct,
            'child_non_reg_pct': strategy.child_non_reg_pct,
        }
        result.append(strategy_dict)

    return result


def _build_family_state(cfg: dict, deps: Optional[SimulationDeps] = None) -> FamilyState:
    """Build FamilyState from configuration data."""
    deps = _resolve_deps(deps)
    family = cfg.get('family', {})
    members = family.get('members', [])
    
    # Find primary and spouse
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    spouse = find_member_by_role(members, 'spouse', {})
    
    # Get tax brackets and calculate marginal rates
    brackets, primary_mtr, spouse_mtr = _get_tax_brackets(cfg, deps)
    
    # Get incomes for savings calculation
    primary_income = primary.get('gross_income', 0)
    spouse_income = spouse.get('gross_income', 0)
    
    # DP#18/5: Savings rate from config, with zero default (not a hardcoded household rate)
    savings_cfg = cfg.get('savings', {})
    savings_rate = savings_cfg.get('rate', 0.0)
    annual_savings = (primary_income + spouse_income) * savings_rate
    
    # Count eligible children
    children = family.get('children', [])
    resp_eligible_children = len([c for c in children if c.get('birth_year', 0) >= 2008])  # Rough eligibility

    # FHSA from primary member (DP#1/DP#28: eligibility computed from dates, not booleans)
    # DP#18/5: FHSA room from config, with zero default (not hardcoded household value)
    fhsa_room = primary.get('fhsa_room_accumulated', 0)
    # DP#18/5: FHSA lifetime limit from config, not hardcoded
    fhsa_lifetime = primary.get('fhsa_lifetime_limit', 0)
    # First-time buyer status: null means never eligible (e.g., used HBP and not restored)
    # A date string means eligible since that date
    fhsa_first_time_since = primary.get('fhsa_first_time_buyer_since')
    if fhsa_first_time_since is None:
        fhsa_room = 0
        fhsa_lifetime = 0

    # DP#18/5: RESP match cap from config, not hardcoded household value
    accounts = cfg.get('accounts', {})
    resp_match_cap = accounts.get('resp_annual_match_cap', 0.0)
    # DP#8/DP#10 (#241): CESG-matched contribution per child from config;
    # 0 falls back to the Canada package figure in the allocation engine.
    resp_contribution_match_max = accounts.get('resp_annual_room_per_child', 0.0)

    return deps.FamilyState(
        primary_income=primary_income,
        spouse_income=spouse_income,
        primary_marginal_rate=primary_mtr,
        spouse_marginal_rate=spouse_mtr,
        primary_rrsp_room=primary.get('rrsp_room_accumulated', 0),
        spouse_rrsp_room=spouse.get('rrsp_room_accumulated', 0),
        primary_tfsa_room=primary.get('tfsa_room_accumulated', 0),
        spouse_tfsa_room=spouse.get('tfsa_room_accumulated', 0),
        fhsa_room=fhsa_room,
        fhsa_lifetime_remaining=fhsa_lifetime,
        resp_eligible_children=resp_eligible_children,
        resp_annual_match_cap=resp_match_cap,
        resp_contribution_match_max=resp_contribution_match_max,
        annual_savings=annual_savings,
        bracket_gap=primary_mtr - spouse_mtr
    )


def _discover_resp_actions(cfg: dict) -> List[str]:
    """Discover RESP actions based on current balance."""
    accounts = cfg.get('accounts', {})
    resp_balance = accounts.get('resp_current_balance', 0)
    
    if resp_balance > 0:
        return ['keep', 'eap', 'collapse']
    else:
        return ['keep']


def _discover_readvanceable_options(cfg: dict, deps: Optional[SimulationDeps] = None) -> List[bool]:
    """Discover readvanceable mortgage strategy options based on trigger conditions."""
    deps = _resolve_deps(deps)
    property_cfg = cfg.get('property', {})
    assumptions = cfg.get('assumptions', {})
    
    # Check if HELOC readvance is enabled
    heloc_readvance = property_cfg.get('heloc_readvance', False)
    if not heloc_readvance:
        return [False]
    
    # Get rates and tax info
    # Issue #654/#677: the HELOC's own declared rate, never aliased from
    # mortgage_rate. heloc_readvance=True was already confirmed above, so
    # this gate's outcome genuinely depends on the resolved value (unlike
    # _discover_strategy_scenarios' inert no-facility case) -- no
    # caller-supplied placeholder here. resolve_heloc_rate's own DP#13
    # fallback (mortgage-rate-derived, logged) only fires for a legacy
    # hand-built config that declares heloc_readvance without ever
    # declaring a HELOC rate; a real contract always declares both
    # together.
    heloc_rate = deps.resolve_heloc_rate(cfg)
    # DP#21: resolved from return_model, the single source of truth (issue
    # #591) -- assumptions.investment_return is a deprecated scalar that can
    # silently disagree with return_model once an overlay has been applied.
    investment_return = deps.resolve_return_rate(cfg)
    capital_gains_inclusion = assumptions.get('capital_gains_inclusion', 0.5)
    
    # Calculate primary marginal tax rate
    _, primary_mtr, _ = _get_tax_brackets(cfg, deps)
    
    # Calculate after-tax costs and returns
    after_tax_heloc_cost = heloc_rate * (1 - primary_mtr)
    after_tax_return = investment_return * (1 - primary_mtr * capital_gains_inclusion)
    
    # Check if the readvanceable mortgage strategy is profitable
    if after_tax_return > after_tax_heloc_cost:
        return [True, False]
    else:
        return [False]


# Issue #735: the candidate fractions of margin_available the sweep tries.
# 0.0 (fully undrawn) is always included, and always FIRST -- it is the
# DP#32-correct default and must be a real, evaluated option, not merely
# the fallback when this dimension is skipped. 1.0 (fully drawn) reproduces
# the engine's pre-#735 behaviour, so the household can see exactly what
# changed and by how much.
DEFAULT_DRAW_FRACTION_CANDIDATES = [0.0, 0.25, 0.5, 1.0]


def _discover_draw_fraction_options(cfg: dict) -> List[float]:
    """Discover draw-fraction candidates for a declared revolving facility
    (issue #735).

    DP#16 trigger: a declared facility with undrawn room
    (``property.margin_available > 0``) is what makes the question
    meaningful at all -- a household with none has nothing to trade off, so
    this returns ``[0.0]`` (a single, un-swept, correct value) rather than
    multiplying every other dimension by 4 for no reason. This mirrors
    ``_discover_readvanceable_options``'s own gating shape.
    """
    margin_available = cfg.get('property', {}).get('margin_available', 0)
    if not margin_available or margin_available <= 0:
        return [0.0]
    return list(DEFAULT_DRAW_FRACTION_CANDIDATES)


def _discover_deduct_later_options(cfg: dict, deps: Optional[SimulationDeps] = None) -> List[bool]:
    """Discover deduct-later options based on bracket gap."""
    deps = _resolve_deps(deps)
    # Calculate marginal rates
    _, primary_mtr, spouse_mtr = _get_tax_brackets(cfg, deps)
    
    # Check bracket gap
    bracket_gap = primary_mtr - spouse_mtr
    
    if bracket_gap > 0:
        return [True, False]
    else:
        return [False]


# Issue #812: the child-allocation biases the discovery sweep compares. Each is
# an AllocationStrategy child_*_pct override; StrategyEngine.allocate_child caps
# it by the child's own room and spills the residual by the #701 waterfall.
# 'room_priority' (all targets 0.0) is the #701-correct default — fill tax-free
# room first — and is listed FIRST so it reads as the recommended baseline, not
# merely the fallback (mirrors DEFAULT_DRAW_FRACTION_CANDIDATES' 0.0-first rule).
_CHILD_ALLOCATION_BIASES = (
    ('room_priority', {}),
    ('tfsa', {'child_tfsa_pct': 1.0}),
    ('fhsa', {'child_fhsa_pct': 1.0}),
    ('rrsp', {'child_rrsp_pct': 1.0}),
    ('non_reg', {'child_non_reg_pct': 1.0}),
)


def _child_savings(child: dict, savings_rate: float, salary_growth: float = 0.0) -> float:
    """A child's OWN incremental savings: their gross income times the household
    savings rate. 0 when the child has no declared income (#701: a child with no
    income of their own has nothing of their own to route — DP#32 absence, not a
    defaulted split)."""
    return child.get('gross_income', 0) * savings_rate


def _sweep_child_allocation(child_state: ChildState,
                           deps: Optional[SimulationDeps] = None) -> List[dict]:
    """Route this child's OWN savings under each _CHILD_ALLOCATION_BIAS (#812).

    This is the discovery-layer sweep deliver #2 asks for: it surfaces, per
    child, where the child's money lands under TFSA-first vs FHSA-first vs
    RRSP-first vs non-reg vs the room-priority default, so the comparison is
    real DATA (computed by the same StrategyEngine the simulation uses), not a
    hardcoded 0.0. Each entry is the ChildAllocationResult for that bias.
    """
    deps = _resolve_deps(deps)
    options = []
    for bias_id, child_pcts in _CHILD_ALLOCATION_BIASES:
        strategy = deps.AllocationStrategy(**child_pcts)
        routing = deps.StrategyEngine(strategy).allocate_child(child_state)
        entry = {'bias': bias_id}
        entry.update(routing.as_dict())
        options.append(entry)
    return options


def _discover_child_accounts(cfg: dict, deps: Optional[SimulationDeps] = None) -> List[dict]:
    """Discover child account options based on accumulated room (#812).

    Each child with room gains an ``allocation_options`` sweep: where the
    child's OWN savings route under each bias (see _sweep_child_allocation).
    The routing is by the child's OWN room and never claims the household's
    deduction (#701).
    """
    deps = _resolve_deps(deps)
    family = cfg.get('family', {})
    children = family.get('children', [])

    # DP#18/5: savings rate from config, zero default (not a household opinion).
    savings_rate = cfg.get('savings', {}).get('rate', 0.0)

    result = []

    for child in children:
        fhsa_room = child.get('fhsa_room_accumulated', 0)
        tfsa_room = child.get('tfsa_room_accumulated', 0)
        rrsp_room = child.get('rrsp_room_accumulated', 0)

        # Include child if they have any room > 0
        if fhsa_room > 0 or tfsa_room > 0 or rrsp_room > 0:
            child_savings = _child_savings(child, savings_rate)
            # DP#1/#701: FHSA lifetime cap from config when declared; absent, the
            # annual accumulated room is the binding cap (do not invent a larger
            # lifetime the child may not have).
            fhsa_lifetime = child.get('fhsa_lifetime_limit', fhsa_room)
            child_state = deps.ChildState(
                savings=child_savings,
                tfsa_room=tfsa_room,
                fhsa_room=fhsa_room,
                fhsa_lifetime_remaining=fhsa_lifetime,
                rrsp_room=rrsp_room,
                name=child.get('name', 'Unknown'),
            )
            result.append({
                'child_name': child.get('name', 'Unknown'),
                'fhsa_room': fhsa_room,
                'tfsa_room': tfsa_room,
                'rrsp_room': rrsp_room,
                'savings': child_savings,
                'allocation_options': _sweep_child_allocation(child_state, deps),
            })

    return result