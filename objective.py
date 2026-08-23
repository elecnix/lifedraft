#!/usr/bin/env python3
"""
Objective Functions — Pluggable optimization objectives (DP#22).

The optimizer ranks, it doesn't choose. The user picks the objective;
the optimizer produces an ordered list.

Objective functions take a list of YearResults and a config dict,
and return a scalar score. Different objectives answer different questions:
- max_net_benefit: "Which strategy leaves me most after tax?"
- max_terminal_wealth: "Which maximizes total PRE-TAX assets at the end?" (not an estate value)
- max_after_tax_estate: "Which strategy leaves my heirs the most, after the
  RRSP/RRIF deemed-disposition tax at death?" (issue #580)
- max_after_tax_income: "Which strategy delivers the most after-tax retirement
  income / drawdown?" (issue #728, DP#22)
- max_probability_success: "What's the probability the plan meets its targets?"
  (Monte Carlo, issue #728, DP#22/DP#29)
- min_retirement_gap: "How close am I to my retirement target?"
- min_years_to_mortgage_free: "When will I be debt-free?"

Usage:
    from objective import max_net_benefit, ObjectiveFunction
    
    # Using a built-in objective
    score = max_net_benefit(results, cfg)
    
    # Creating a custom objective
    obj = ObjectiveFunction(name="my_objective", fn=lambda r, c: r[-1].total_assets - r[-1].total_debt)
    score = obj.evaluate(results, cfg)
"""

import math

from tax_data import default_tax_provider
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
# DP#25 (issue #332): import from the canonical data/config layer, not the
# simulation engine. YearResult and SimulationConfig are both defined in
# simulation_config.py and only re-exported through simulation.py. objective.py
# lives in the optimization layer and must depend inward (on data/config), so
# it imports directly from simulation_config rather than the simulation engine.
from simulation_config import SimulationConfig
from year_result import YearResult
# DP#25 (issue #732): the estate tax math lives in the jurisdiction package;
# the optimization layer resolves it through the provider registry seam
# instead of importing countries.canada.estate directly. objective.py has
# zero `countries` imports — the estate adapter is injected here.
from jurisdiction_providers import get_provider
_ESTATE_PROVIDER = get_provider('estate')
_compute_estate = _ESTATE_PROVIDER['compute_estate']
# #705: the couple -> death-ordered terminal-return list mapping, resolved
# through the same seam (it reads the ITA couple plan fields), so this module
# assembles the per-member list compute_estate now takes without a countries
# import (DP#25).
_couple_terminal_returns = _ESTATE_PROVIDER['couple_terminal_returns']
_EstatePlan = _ESTATE_PROVIDER['EstatePlan']
_EstateResult = _ESTATE_PROVIDER['EstateResult']
# epic #841 bite 4: per-member after-tax net worth, resolved through the same
# seam so this module keeps zero `countries` imports (DP#25).
_after_tax_networth_of_own_accounts = _ESTATE_PROVIDER['after_tax_networth_of_own_accounts']
# #694: CCA recapture on a rental's deemed disposition at death, resolved through
# the same seam (the ITA s.13(1) arithmetic lives in the jurisdiction module).
_recapture_on_disposition = _ESTATE_PROVIDER['recapture_on_disposition']


@dataclass
class ObjectiveFunction:
    """A named, callable objective function.
    
    DP#8: compose through data. Objective functions are data objects
    passed to the optimizer, not hardcoded metrics.
    DP#22: the optimizer ranks, it doesn't choose.
    
    Args:
        name: Human-readable name (for reporting)
        fn: Callable (List[YearResult], Dict) -> float -- the per-path score.
            For deterministic (single-path) optimizers this IS the ranking
            score. For MonteCarloOptimizer the per-path ``fn`` score is
            collected across N seeds and aggregated by
            ``rank_from_distribution`` (default: mean) into the ranking score.
        description: What this objective measures
        rank_from_distribution: Optional Callable (List[float], Dict) -> float
            that aggregates the per-path scores from a Monte Carlo run into a
            single ranking score. Default ``None`` -> ``statistics.mean`` (the
            historical behaviour: rank by expected value). Issue #728
            (DP#22/DP#29): ``max_probability_success`` overrides this to rank
            by the fraction of paths whose per-path score is 1.0 (i.e. the
            probability the plan meets its targets), so a Monte Carlo run
            ranks strategies by P(success) instead of by expected net benefit.
            The per-path ``fn`` returns 1.0 / 0.0 (success / failure) for that
            path, and ``rank_from_distribution`` averages those into a
            probability in [0, 1].
    """
    name: str
    fn: Callable
    description: str = ""
    rank_from_distribution: Optional[Callable] = None
    
    def evaluate(self, results: List[YearResult], cfg: Dict = None) -> float:
        """Evaluate the objective on a simulation result.
        
        Args:
            results: Year-by-year simulation results
            cfg: Optional config dict for context (e.g., retirement target)
        
        Returns:
            Scalar score (higher is better for ranking)
        """
        if not results:
            return 0.0
        return self.fn(results, cfg or {})

    def aggregate(self, scores: List[float], cfg: Dict = None) -> float:
        """Aggregate per-path Monte Carlo scores into a ranking score.

        Issue #728 (DP#22/DP#29): MonteCarloOptimizer calls this with the N
        per-path ``fn`` scores instead of unconditionally averaging them, so
        an objective whose per-path score is a success indicator
        (``max_probability_success``: 1.0 / 0.0) ranks by P(success) rather
        than by expected value. Objectives without ``rank_from_distribution``
        keep the historical mean -> expected-value ranking.
        """
        if not scores:
            return 0.0
        if self.rank_from_distribution is not None:
            return self.rank_from_distribution(scores, cfg or {})
        import statistics
        return statistics.mean(scores)


# ── Built-in objectives ────────────────────────────────────────────────────

def _net_benefit(results: List[YearResult], cfg: Dict) -> float:
    """Net benefit = total assets - total debt at end of simulation,
    minus future taxes (RRSP withdrawal, capital gains, RESP EAP).
    
    Delegates to optimize.compute_net_benefit for comprehensive tax handling
    including retirement drawdown analysis, ACB tracking, and RESP EAP tax.
    """
    from optimize import compute_net_benefit as _cnb
    return _cnb(results, cfg)


def _terminal_wealth(results: List[YearResult], cfg: Dict) -> float:
    """Total after-tax wealth at the end of the projection.
    
    Does NOT deduct future tax on RRSP withdrawals.
    Useful for comparing growth strategies without withdrawal assumptions.
    """
    final = results[-1]
    return final.total_assets - final.total_debt


def _total_tax_savings(results: List[YearResult], cfg: Dict) -> float:
    """Sum of all tax savings (RRSP deductions + every s.20(1)(c) interest
    deduction).

    Measures pure tax efficiency, ignoring investment growth.

    Issue #850: ``traced_borrowing_tax_savings`` is the deduction on the
    mortgage ADVANCE and on the DRAWN revolving line -- two s.20(1)(c) legs
    this engine priced nowhere before #850, and the two that #849's question is
    entirely about. Omitting them here would make "total tax savings" mean
    "some of the tax savings". 0.0 for a household that borrowed no lump sum.
    """
    return (sum(r.rrsp_tax_savings for r in results)
            + sum(r.readvance_tax_savings for r in results)
            + sum(r.traced_borrowing_tax_savings for r in results))


def _retirement_gap(results: List[YearResult], cfg: Dict) -> float:
    """Gap between final wealth and a retirement target.
    
    Returns negative value (score to maximize = -gap).
    Target comes from cfg['retirement_target'] or defaults to $500,000.
    
    A score of 0 means the target is met.
    A positive score means the target is exceeded.
    """
    target = cfg.get('retirement_target', 500000)
    final = results[-1]
    net = final.total_assets - final.total_debt
    return net - target  # Positive = surplus, negative = gap


def _years_to_mortgage_free(results: List[YearResult], cfg: Dict) -> float:
    """Number of years until mortgage is fully paid off.
    
    Returns negative (to maximize = earlier payoff).
    Uses -years so maximizing this = minimizing years.
    If already paid off in year 1, returns -1.
    """
    for i, r in enumerate(results):
        if r.mortgage_balance <= 0:
            return -(i + 1)  # Negative: fewer years is better
    # Not paid off within projection
    return -len(results)


def _readvance_tax_savings_total(results: List[YearResult], cfg: Dict) -> float:
    """Total Smith Manoeuvre tax savings across all years.

    Measures whether SM is worth it in pure tax terms.
    """
    return sum(r.readvance_tax_savings for r in results)


#: Epic #603 Track C Phase 2c (issue #600): the elections used when a config
#: carries NO ``estate`` block at all. Every one of these was, before Phase 2c,
#: hardcoded into ``estate.compute_estate``'s arithmetic and unrepresentable in
#: config; they are preserved here EXACTLY so that a caller who has not migrated
#: gets the same number they got before, and so that the model_fidelity caveat
#: (``estate_elections_not_declared``) has something true to describe. A config
#: sourced from the input contract ALWAYS declares its own estate block --
#: ``estate`` is a required key of the schema -- so these apply only to in-memory
#: internal-shape configs (tests, ad-hoc drivers), never to a real run.
_UNDECLARED_ESTATE_DEFAULTS = {
    'spousal_rollover': True,          # what the old docstring CLAIMED it did
    'primary_dies_first': True,
    'registered_rolled_fraction': 0.0,  # ...but what the old code actually DID:
    'non_reg_rolled_fraction': 0.0,     # two separate returns (see estate.py)
    'tfsa_successor_holder': True,
    'non_reg_primary_share': 0.5,       # the hardcoded 50/50 guess
    'property_primary_share': 0.5,
    'life_insurance_death_benefit': 0.0,
    'taxable_property_fmv': 0.0,
    'taxable_property_acb': 0.0,
    'principal_residence_designated': True,
    'principal_residence_fmv': 0.0,
    'principal_residence_acb': 0.0,
    # Issue #963 (epic #956 bite F): None = a static value (byte-identical to
    # today, DP#32) -- the objective layer's absence-test returns
    # `principal_residence_fmv` unchanged and never reads a rate. Matches
    # EstatePlan.principal_residence_appreciation_rate's default.
    'principal_residence_appreciation_rate': None,
}


def plan_from_config(cfg: Dict):
    """Build a ``countries.canada.estate.EstatePlan`` from the config's
    ``estate`` block (epic #603 Track C Phase 2c, issue #600).

    ``input_contract._map_estate`` produces that block from the contract's
    ``estate`` namespace plus the per-account ``beneficiary``/
    ``successor_holder`` designations, the per-property
    ``designated_principal_residence_years``, the ``life_insurance[]`` list and
    ``assumptions.mortality``. Before Phase 2c every one of those was a silent
    assumption baked into ``estate.compute_estate``, and every one resolved in
    the favourable direction (#600, measured by agent Inky in PR #616).

    When ``cfg`` has no ``estate`` block, ``_UNDECLARED_ESTATE_DEFAULTS`` are
    used -- reproducing the pre-Phase-2c numbers exactly -- and the
    ``estate_elections_not_declared`` approximation fires in the output, so the
    absence is DISCLOSED rather than passed off as a decision (DP#32).
    """
    # Issue #732 (DP#25): EstatePlan is resolved through the jurisdiction
    # provider seam, not imported from countries.canada.estate here.
    # DP#32: explicit absence-test, never truthiness. An estate block that is
    # present but empty ({}) is not the same fact as one that was never
    # supplied -- both end up on the defaults here, but `estate_is_declared`
    # below must be able to tell them apart to decide whether to DISCLOSE that.
    declared = cfg.get('estate')
    declared = {} if declared is None else declared
    fields = {**_UNDECLARED_ESTATE_DEFAULTS, **declared}
    return _EstatePlan(**fields)


def estate_is_declared(cfg: Dict) -> bool:
    """Whether the config actually DECLARED its estate elections, or is running
    on ``_UNDECLARED_ESTATE_DEFAULTS``. Read by model_fidelity to decide whether
    to disclose the defaults (#585/#600)."""
    declared = cfg.get('estate')
    return declared is not None and len(declared) > 0


def compute_after_tax_estate(results: List[YearResult], cfg: Dict):
    """After-tax estate at the terminal year (Canada): ITA s.70(5)/s.146(8.8)
    deemed disposition applied to the terminal balance sheet.

    Issue #580: ``_terminal_wealth``/``_net_benefit`` sum RRSP, TFSA, and
    non-reg dollars at face value, as if they were interchangeable. They are
    not: an RRSP/RRIF dollar is included as ordinary income on the deceased's
    terminal return (often ~50% at the top Quebec bracket) before a
    beneficiary sees it; a TFSA dollar and principal-residence equity pass
    tax-free; a non-reg dollar carries capital-gains tax on its accrued
    (FMV - ACB) portion only. This function maps the terminal ``YearResult``
    onto ``countries.canada.estate.compute_estate`` (PR #574, DP#10 -- the
    estate tax math lives there, not here) so a ranking can reflect what
    actually reaches the heirs rather than a pre-tax fiction.

    **Epic #603 Track C Phase 2c (issue #600): the five estate assumptions this
    function used to make are now INPUTS.** They were: an assumed spousal
    rollover (with declining it literally inexpressible), an assumed TFSA
    successor holder, a hardcoded 50/50 non-registered gain split, an assumed
    principal-residence designation, and a house FMV that silently fell back to
    $0. All five now come from the contract's ``estate`` namespace via
    ``plan_from_config``. What remains defaulted here is only the bracket
    context:

      - **Tax year / brackets**: ``cfg['tax']['year']`` (explicit calendar
        year) if given; else ``cfg['tax']['start_year'] + len(results) - 1``
        (``YearResult.year`` is a 1-indexed *relative* offset, not a calendar
        year, so the terminal calendar year has to be derived from the
        projection's start year); else 2026. **Province**:
        ``cfg['tax']['province']``, default ``'quebec'``.
      - **House FMV**: ``cfg['property']['house_value']``. Still read from the
        property block (the simulation needs it for LTV anyway), but it is no
        longer a silent $0: a contract document always declares its
        ``properties[]``, and whether the residence's GAIN is exempt is now the
        declared ``principal_residence_designated`` election rather than an
        assumption.

    Args:
        results: year-by-year simulation results; only the terminal year is
            used (the estate is a point-in-time balance-sheet valuation).
        cfg: context dict. Reads ``cfg['estate']`` (the elections),
            ``cfg['property']['house_value']`` and
            ``cfg['tax']['province']``/``['year']``/``['start_year']``.

    Returns:
        ``countries.canada.estate.EstateResult`` -- the full gross/tax/net
        breakdown, carrying the elections that produced it. Callers that only
        need a scalar score use ``.net_estate`` (``_after_tax_estate`` below).
    """
    # Issue #732 (DP#25): compute_estate and EstateResult are resolved
    # through the jurisdiction provider seam at module load, not imported
    # from countries.canada.estate here. The shared arg-prep lives in
    # ``_estate_call_args`` so a reached caller that injects ``compute_estate``
    # directly (optimize.py, which may import the jurisdiction package) can
    # build the SAME call without restating the prep -- one source for the
    # mapping of a terminal YearResult onto compute_estate's parameters.
    args = _estate_call_args(results, cfg)
    if args is None:
        return _EstateResult()
    return _compute_estate(**args)


def _terminal_calendar_year(results: List[YearResult], cfg: Dict) -> int:
    """The projection's terminal CALENDAR year (DP#9 -- one spelling).

    ``YearResult.year`` is a 1-indexed *relative* offset (see
    simulation_state.py ``YearResult(year=year + 1, ...)``), not a calendar
    year -- the terminal calendar year is derived from ``start_year``, never
    read off ``final.year`` directly. The estate's deemed-disposition FMV and
    the terminal brackets both value the household on this SAME year.
    """
    tax_cfg = cfg.get('tax', {}) or {}
    explicit_year = tax_cfg.get('year')
    start_year = tax_cfg.get('start_year')
    if explicit_year is not None:
        return explicit_year
    if start_year is not None:
        return start_year + len(results) - 1
    return 2026


def _qc_provincial_brackets(terminal_cal_year: int, cfg: Dict) -> Optional[list]:
    """The PROVINCIAL slice of the terminal-year brackets (#1035).

    The QC investment-expense carry-forward released at death is a Quebec-only
    deduction, so it is valued on the provincial brackets alone -- never on the
    combined federal+provincial list. ``None`` when the provider has no data
    for the year (compute_estate then refuses loudly if a carry-forward is
    actually present, DP#32; a zero carry-forward never needs it).
    """
    tax_cfg = cfg.get('tax', {}) or {}
    province = tax_cfg.get('province', 'quebec')
    try:
        _, provincial = default_tax_provider().get_split_brackets(
            terminal_cal_year, province=province)
        return provincial
    except ValueError:
        return None


def _terminal_brackets(results: List[YearResult], cfg: Dict) -> list:
    """The combined federal+provincial brackets for the projection's terminal
    year, derived identically for the estate and the per-member family net
    worth so the two value the SAME household on the SAME tax year (DP#9 -- one
    spelling of the year/province resolution).

    ``YearResult.year`` is a 1-indexed *relative* offset (see
    simulation_state.py ``YearResult(year=year + 1, ...)``), not a calendar
    year -- the terminal calendar year is derived from ``start_year``, never
    read off ``final.year`` directly.
    """
    tax_cfg = cfg.get('tax', {}) or {}
    province = tax_cfg.get('province', 'quebec')
    return default_tax_provider().get_combined_brackets(
        _terminal_calendar_year(results, cfg), province)


def _cca_recapture_for(final: YearResult, cfg: Dict,
                         terminal_cal_year: Optional[int] = None) -> float:
    """Total COUPLE-level CCA recapture at the deemed disposition of every rental
    that elected CCA (#694, ITA s.13(1)).

    For each rental with a ``cca`` election, recapture the previously-claimed CCA:
    the disposition proceeds (up to the original capital cost) less the terminal
    UCC the fold tracked (``final.rental_ucc``, falling back to the declared
    opening UCC if the property never reached the fold's UCC ledger). Ordinary
    income (100% inclusion); the estate stacks it on the terminal return. The
    capital GAIN on the same property is NOT here -- ``compute_estate`` already
    taxes the whole (FMV - ACB) gain via ``taxable_property_*`` (DP#9: recapture
    and gain are distinct bases). 0.0 for a household with no CCA election.

    Issue #964: a rental SOLD on/before the terminal (death) year is not owned
    at death -- its CCA recapture was already realized at its mid-horizon sale
    (the disposition rule prices it), so it must not be recaptured AGAIN at the
    deemed disposition. ``terminal_cal_year`` is the estate's terminal calendar
    year (``start_year + len(results) - 1``), passed by ``_estate_call_args``
    so the two value the estate on the SAME year; None -> conservatively keep
    every rental (do not drop a recapture on an unknown terminal year).
    """
    props = cfg.get('properties')
    if not props:
        return 0.0  # no properties -> no rental, no recapture
    ucc_by_prop = getattr(final, 'rental_ucc', None)
    if ucc_by_prop is None:
        ucc_by_prop = {}  # a YearResult built without the fold's UCC ledger
    total = 0.0
    for prop in props:
        if not isinstance(prop, dict):
            continue
        # Issue #964: a property sold on/before the terminal year is not owned
        # at death -- skip it (its recapture was realized at the sale).
        sale = prop.get('sale')
        if sale is not None and terminal_cal_year is not None:
            # The internal `sale` block always carries the resolved calendar
            # `year` (carried by `input_contract._map_property_sale`, which
            # resolves `date`->`year` at map time) -- read it directly.
            if sale['year'] <= terminal_cal_year:
                continue
        rental = prop.get('rental')
        cca = rental.get('cca') if rental else None
        if not cca:
            continue
        ucc = ucc_by_prop.get(prop['id'], cca['opening_ucc'])
        total += _recapture_on_disposition(
            cca['fmv_at_disposition'], cca['capital_cost'], ucc)['recapture']
    return total


def _estate_call_args(results: List[YearResult], cfg: Dict) -> Optional[Dict]:
    """Build the keyword arguments ``compute_estate`` expects from the
    terminal ``YearResult`` and config (issue #732, DP#25).

    Pure data assembly -- no jurisdiction import. Shared by
    ``compute_after_tax_estate`` (which resolves ``compute_estate`` through
    the provider seam) and by any reached caller that injects
    ``compute_estate`` directly. Returns ``None`` for empty ``results`` so
    the caller can produce an empty ``EstateResult`` without calling the
    estate function.
    """
    if not results:
        return None

    final = results[-1]
    prop = cfg.get('property', {}) or {}
    # DP#32: explicit absence-test, not `or {}` -- the estate block has a
    # strict loader (`config_access._estate_block` raises on an explicit
    # null), so here it is either a dict (possibly empty) or absent. An empty
    # dict and an absent key both fall to `_UNDECLARED_ESTATE_DEFAULTS` via
    # `plan_from_config` (symmetric), but reading the rate off it must use the
    # absence-test, not truthiness, so a future field that distinguishes
    # "declared empty" from "absent" is not silently laundered. Mirrors
    # `_estate_block`'s own contract.
    estate_cfg = cfg.get('estate')
    estate_cfg = {} if estate_cfg is None else estate_cfg
    house_value = prop.get('house_value', 0.0)
    # The terminal calendar year -- the fold's actual last year (DP#1: store
    # the date, derive the year). Read from `assumptions.start_year` (where
    # `to_internal_config` puts it), NOT `_terminal_calendar_year` (which
    # resolves the TAX year off `cfg['tax']` -- a different key that holds
    # province/country, not the projection span). Shared by the appreciation
    # compounding below and the #964 sold-property exclusion so both value the
    # estate on the SAME terminal year.
    sim_start = cfg.get('assumptions', {}).get('start_year', 2026)
    terminal_cal_year = sim_start + len(results) - 1
    # Issue #964: a principal residence SOLD on/before the terminal (death)
    # year is NOT owned at death -- the `principal_disposition` rule already
    # converted it to portfolio cash (reinvested proceeds), so the estate must
    # not value it AGAIN at its death-year deemed disposition (the double-count
    # this issue is about). `input_contract._map_estate` already zeros the
    # estate block's `principal_residence_fmv`/`house_equity` for a sold
    # principal; this layer INDEPENDENTLY re-derives the home's value from
    # `cfg['property']['house_value']` (and compounds it), so it must apply the
    # SAME exclusion or the two spellings disagree and the estate values a home
    # the household no longer owns. `principal_sale.year` is the sale calendar
    # year (carried by `_map_principal_sale`); a sale beyond the terminal year
    # never fires inside the projection -> the home IS still owned at death ->
    # keep it. The mortgage is discharged at the sale (the disposition rule
    # retires it), so a sold principal contributes neither value nor debt to
    # the terminal estate.
    principal_sale = prop.get('principal_sale')
    principal_sold = False
    if principal_sale is not None:
        # The internal `principal_sale` block always carries the resolved
        # calendar `year` (carried by `input_contract._map_principal_sale`,
        # which resolves `date`->`year` at map time) -- read it directly.
        principal_sold = principal_sale['year'] <= terminal_cal_year
    if principal_sold:
        house_value = 0.0
    # Issue #963 (epic #956 bite F): the principal residence's value at the
    # terminal deemed disposition is its APPRECIATED value, not the static
    # `house_value` -- the home compounds year over year exactly as it does in
    # the annual LTV/charge math and Bite E's sale (DP#9 -- one spelling of
    # the appreciated value, the same `(1 + rate) ** (cal_year - start_year)`
    # compounding `simulation_rules._principal_value_for_year` uses). The
    # terminal calendar year is the fold's actual last year
    # (`assumptions.start_year + len(results) - 1`), so the exponent is
    # `len(results) - 1` years of growth.
    #
    # The rate is read from the ESTATE block
    # (`estate.principal_residence_appreciation_rate`, carried there from the
    # principal property by `input_contract._map_estate`) so the estate's
    # property data is self-describing -- the deemed disposition compounds
    # using the rate the estate block itself carries, not a pointer to the
    # `property` block (DP#9, one spelling the estate consumes). The
    # `property.appreciation_rate` leaf is read as a FALLBACK for robustness
    # (an overlay that moves the property rate without rebuilding the estate
    # block still flows through); in a normal `to_internal_config` run both
    # carry the SAME value, so the fallback is inert. Absence-safe (DP#32): an
    # absent or 0.0 rate (the common case, incl. the golden fixture whose
    # legacy `property`/`estate` dicts never carry this key) returns the
    # static `house_value` and never reads the exponent, so the estate is
    # byte-identical to today. The ACB and the PRE exemption fraction are
    # unaffected (appreciation grows the gain base, not the cost base -- DP#19).
    appreciation_rate = estate_cfg.get('principal_residence_appreciation_rate')
    if appreciation_rate is None:
        appreciation_rate = prop.get('appreciation_rate')
    if appreciation_rate is not None and appreciation_rate != 0.0:
        # The exponent is `len(results) - 1` years of growth, matching
        # `simulation_rules._principal_value_for_year`'s
        # `(1 + rate) ** (cal_year - start_year)` exactly (DP#9 -- one spelling).
        # `terminal_cal_year` / `sim_start` are computed above (shared with the
        # #964 sold-principal exclusion).
        house_value = house_value * (
            (1.0 + appreciation_rate) ** max(0, terminal_cal_year - sim_start))
    brackets = _terminal_brackets(results, cfg)

    mortgage = final.mortgage_balance
    house_equity = max(0.0, house_value - mortgage)
    # `total_debt` (SimState.total_debt()) already includes the mortgage; it
    # is netted once via house_equity above, so only the *remaining* debt
    # (HELOC / readvanced HELOC) is passed as compute_estate's `debts` --
    # passing total_debt there too would subtract the mortgage a second time.
    other_debt = max(0.0, final.total_debt - mortgage)

    plan = plan_from_config(cfg)
    # The principal residence's FMV is the gain base when it is NOT designated
    # (ITA s.40(2)(b) applies only to designated years). It comes from the same
    # property block the simulation uses for LTV, so the two cannot disagree --
    # `_map_estate` records the contract's value, and this keeps it in sync with
    # whatever the running config actually holds (an overlay can move it).
    if not plan.principal_residence_designated:
        from dataclasses import replace as _replace
        plan = _replace(plan, principal_residence_fmv=house_value)

    # Issue #695: when the per-year PRE allocation is active, the principal
    # residence's gain base is one entry in plan.property_gains. Keep its FMV in
    # sync with the SAME house_value the simulation uses for LTV (an overlay can
    # move it), exactly as principal_residence_fmv is synced above -- the two
    # spellings of the home's value must not disagree (DP#9). The contract's ACB
    # and the exemption fraction are unaffected.
    if plan.property_gains is not None:
        from dataclasses import replace as _replace
        synced = tuple(
            {**g, 'fmv': house_value} if g['is_principal'] else g
            for g in plan.property_gains)
        plan = _replace(plan, property_gains=synced)

    # #694 (epic #690 bite 3): a rental that elected CCA has its previously-
    # claimed depreciation RECAPTURED as ordinary income at the deemed disposition
    # (ITA s.13(1)). The recapture uses the terminal UCC the fold tracked
    # (final.rental_ucc) against the couple's disposition proceeds and capital
    # cost -- carried on each rental's internal `cca` block. Threaded onto the
    # plan so compute_estate taxes it (100% inclusion, stacked on the terminal
    # return). 0.0 -> byte-identical estate for a household with no CCA election.
    recapture = _cca_recapture_for(final, cfg, terminal_cal_year)
    if recapture > 0.0:
        from dataclasses import replace as _replace
        plan = _replace(plan, cca_recapture=recapture)

    # #705: compute_estate now sums the deemed-disposition tax over a per-member
    # LIST of terminal returns, not two hardcoded scalars. For the two-adult
    # household the two owners' registered balances (primary's RRSP + LIF + LIRA;
    # spouse's RRSP + spousal RRSP) map, via the couple/plan mapper, onto the
    # death-ordered pair of returns -- byte-identical to the old first/second
    # split (a multi-generation household would assemble a longer list here).
    members = _couple_terminal_returns(
        registered_primary=final.primary_rrsp + final.lif_balance + final.lira_balance,
        registered_spouse=final.spouse_rrsp + final.spousal_rrsp,
        plan=plan,
    )
    return {
        'members': members,
        'tfsa': final.total_tfsa,
        'non_reg_fmv': final.non_reg_balance,
        'non_reg_acb': final.non_reg_acb,
        'house_equity': house_equity,
        'debts': other_debt,
        'brackets': brackets,
        'plan': plan,
        # Issue #1031: the Smith-Manoeuvre investment SLEEVE -- a leveraged
        # non-reg portfolio tracked separately from ``non_reg_balance``. The
        # HELOC that financed it is already in ``other_debt`` (subtracted as a
        # debt); pre-#1031 the estate subtracted that debt but ignored this
        # asset, understating a leveraged household's estate by the whole SM
        # portfolio net of its deemed-disposition tax. Priced by
        # ``compute_estate`` as a second capital-property pot mirroring non-reg
        # (same ownership split + rollover). 0.0 for a household with no SM
        # sleeve (the golden household) -> byte-identical (DP#32).
        'sm_investment_fmv': final.sm_investment_balance,
        'sm_investment_acb': final.sm_investment_cost_basis,
        # Issue #1035: the QC investment-expense carry-forward the annual cap
        # stranded (TA s.336.0.1 allows applying it in a later year INCLUDING
        # the year of death). The terminal deemed disposition's taxable gains
        # on the non-reg + SM pots are investment income, so compute_estate
        # releases it there -- valued on the PROVINCIAL slice of the same
        # terminal-year brackets (a Quebec-only deduction must not be valued
        # on the combined list). 0.0 for households that never stranded
        # anything -> byte-identical estate (DP#32).
        'qc_carry_forward': final.sm_qc_carry_forward,
        'qc_provincial_brackets': _qc_provincial_brackets(
            terminal_cal_year, cfg),
    }


def _after_tax_estate(results: List[YearResult], cfg: Dict) -> float:
    """After-tax estate value to maximize -- see ``compute_after_tax_estate``."""
    return compute_after_tax_estate(results, cfg).net_estate


def _children_after_tax_networth(results: List[YearResult], cfg: Dict) -> float:
    """After-tax net worth of the CHILDREN's OWN accounts at the horizon
    (epic #841 bite 4).

    Bite 1/2 made each child a first-class savings subject with their OWN
    registered/non-registered accounts, threaded through the per-year fold on
    ``jurisdiction_state['canada']['child_accounts']`` and surfaced on the
    terminal ``YearResult.child_accounts``. Bite 2 deliberately kept those out
    of ``total_assets()`` (the household view), so this is the piece the FAMILY
    total must add back: each child's after-tax net worth under the same
    deemed-disposition rules the adults' estate uses, via the jurisdiction
    seam's ``after_tax_networth_of_own_accounts`` (DP#9/DP#25 -- no second
    spelling of the death-tax math, no ``countries`` import here).

    0.0 for a household with no child-savers (empty list, or all-zero entries
    like the golden household's RESP-only children) -- a hard, modelled zero,
    never a fabricated contribution (DP#32). Uses the SAME terminal brackets as
    the estate, so every family member is valued on one tax year.
    """
    if not results:
        return 0.0
    child_accounts = getattr(results[-1], 'child_accounts', None) or []
    if not child_accounts:
        return 0.0
    brackets = _terminal_brackets(results, cfg)
    return sum(
        _after_tax_networth_of_own_accounts(
            rrsp=acct.get('rrsp_balance', 0.0),
            tfsa=acct.get('tfsa_balance', 0.0),
            fhsa=acct.get('fhsa_balance', 0.0),
            non_reg_fmv=acct.get('non_reg_balance', 0.0),
            non_reg_acb=acct.get('non_reg_acb', 0.0),
            brackets=brackets,
        )
        for acct in child_accounts
    )


def _extra_adults_after_tax_networth(results: List[YearResult], cfg: Dict) -> float:
    """Issue #899 (part a): after-tax net worth of each ADDITIONAL accumulating
    adult's OWN accounts at the horizon.

    The compute uncap (#899) gives an adult beyond the primary couple their OWN
    seeded/grown RRSP/TFSA, threaded through the fold on
    ``jurisdiction_state['canada']['adult_rrsp'/'adult_tfsa']`` (slots >= 2) and
    surfaced on the terminal ``YearResult.extra_adult_accounts``. Like the
    child-savers (bite 4), those balances are deliberately kept OUT of the
    two-slot ``total_assets`` so the golden two-adult household is byte-identical;
    this is the piece the FAMILY total adds back, valuing each extra adult's
    wealth under the SAME deemed-disposition rules the estate uses (via the
    jurisdiction seam -- DP#9/DP#25, no second spelling of the death-tax math).

    0.0 for a two-adult household (empty list) -- a modelled zero, never a
    fabricated balance (DP#32). Uses the SAME terminal brackets as the estate."""
    if not results:
        return 0.0
    extra = getattr(results[-1], 'extra_adult_accounts', None) or []
    if not extra:
        return 0.0
    brackets = _terminal_brackets(results, cfg)
    return sum(
        _after_tax_networth_of_own_accounts(
            rrsp=acct.get('rrsp_balance', 0.0),
            tfsa=acct.get('tfsa_balance', 0.0),
            fhsa=0.0,
            non_reg_fmv=0.0,
            non_reg_acb=0.0,
            brackets=brackets,
        )
        for acct in extra
    )


def _family_after_tax_networth(results: List[YearResult], cfg: Dict) -> float:
    """Whole-family after-tax net worth / estate at the horizon (epic #841
    bite 4, DP#22): ONE number across ALL members.

    = the household's after-tax estate (both adults, with deemed disposition,
    spousal rollover, principal-residence exemption, non-reg gains and all debt
    already handled by ``compute_after_tax_estate`` -- reused unchanged, so this
    invents no new tax and the household side is bit-identical to
    ``max_after_tax_estate``)
    + each CHILD's own after-tax net worth (``_children_after_tax_networth``).

    For a household with no child-savers the second term is 0.0, so this equals
    ``max_after_tax_estate`` exactly -- the golden household ranks identically
    on either objective. The two diverge precisely when a strategy grows a
    child's wealth, which is the point of ranking the family as one unit.
    """
    return (compute_after_tax_estate(results, cfg).net_estate
            + _children_after_tax_networth(results, cfg)
            # Issue #899 (part a): additional accumulating adults are family
            # members too -- 0.0 for a two-adult household, so this term leaves
            # every existing household ranking bit-identical (DP#32).
            + _extra_adults_after_tax_networth(results, cfg))


def family_balance_sheet(results: List[YearResult], cfg: Dict) -> Dict:
    """The family's after-tax net worth DECOMPOSED per member, with intra-family
    LOANS booked (issue #859 Part A -- the home bite 3 (#858) deferred here).

    Bite 3 shipped the parent->child GIFT and deferred the LOAN variant: a
    funding loan differs from a gift only in that the parent keeps a RECEIVABLE
    instead of giving the money away, and with no family-wide net-worth view a
    loan was indistinguishable from a gift -- it would misstate DP#18 (a loan
    must NOT reduce the lender's net worth: the receivable offsets it). Bite 4
    (#861) built that view (``_family_after_tax_networth``). This function is the
    balance-sheet decomposition on top of it, booking a loan-funded child
    contribution correctly:

    - **Lender (household) side**: the household's after-tax estate PLUS the
      loans it has extended to its children (``loans_receivable``). The estate
      already dropped by the money carved out to fund the child's room; adding
      the receivable back restores the lender's net worth -- lending is not
      giving (DP#18). For a plain GIFT there is no receivable, so the lender's
      net worth stays reduced, exactly as it should.
    - **Child side**: each child's own after-tax net worth (via the SAME
      deemed-disposition seam the estate uses, DP#9) LESS the loan principal the
      child owes (``loan_liability`` = the ``loan_funded_principal`` the fold
      accumulated, self-limited to the child's accrued room, #857).

    The family TOTAL is invariant to loan-vs-gift: an intra-family loan is a wash
    (Σ receivable == Σ liability), so ``family_total`` equals
    ``_family_after_tax_networth`` EXACTLY (DP#9 -- one spelling of the family
    number; the balance sheet only re-attributes it across members). Returns a
    modelled all-zero loan book when no repayable transfer is declared (DP#32).
    """
    household_estate = compute_after_tax_estate(results, cfg).net_estate
    child_accounts = (getattr(results[-1], 'child_accounts', None)
                      if results else None) or []
    brackets = _terminal_brackets(results, cfg) if results else []
    children = []
    receivable_total = 0.0
    for acct in child_accounts:
        own_nw = _after_tax_networth_of_own_accounts(
            rrsp=acct.get('rrsp_balance', 0.0),
            tfsa=acct.get('tfsa_balance', 0.0),
            fhsa=acct.get('fhsa_balance', 0.0),
            non_reg_fmv=acct.get('non_reg_balance', 0.0),
            non_reg_acb=acct.get('non_reg_acb', 0.0),
            brackets=brackets,
        )
        liability = acct.get('loan_funded_principal', 0.0)
        receivable_total += liability
        children.append({
            'own_after_tax_networth': own_nw,
            'loan_liability': liability,
            'net_worth': own_nw - liability,
        })
    # The receivable is held by the household (the lender is an adult member);
    # the extra accumulating adults (#899) carry no receivable of their own here.
    household_net_worth = household_estate + receivable_total
    extra_adults = _extra_adults_after_tax_networth(results, cfg)
    family_total = (household_net_worth
                    + sum(c['net_worth'] for c in children)
                    + extra_adults)
    return {
        'household_after_tax_estate': household_estate,
        'loans_receivable': receivable_total,
        'household_net_worth': household_net_worth,
        'children': children,
        'extra_adults_after_tax_networth': extra_adults,
        'family_total': family_total,
    }


# Issue #728 (DP#22): max_after_tax_income and max_probability_success are the
# two ObjectiveFunction instances DP#22 names that were documented-but-missing.
# Both reuse EXISTING machinery read-only -- the retirement drawdown's
# after-tax outputs (surfaced on YearResult by simulation_rules /
# retirement_transition, which this effort must NOT edit) and the Monte Carlo
# return-model path (monte_carlo_optimizer.py, which this effort wires but does
# not duplicate). No new drawdown math, no new return model.

def _after_tax_income(results: List[YearResult], cfg: Dict) -> float:
    """Total AFTER-TAX retirement income delivered across the projection.

    Issue #728 (DP#22): "rank strategies by after-tax retirement income /
    drawdown." This consumes the drawdown machinery's outputs read-only -- it
    sums two fields the retirement_transition / simulation_rules rules surface
    onto ``YearResult`` every retired year:

      - ``after_tax_income``: employment income net of tax (pre-retirement it
        is the working salary net of tax; in a retired year it is whatever
        employment income remains, net of tax).
      - ``drawdown_net_delivered``: the NET (after-tax) dollars the registered
        / non-reg / TFSA / LIF drawdown actually delivered toward the year's
        declared ``drawdown_net_target`` spending need (issues #363/#579).
        Equal to the target within tolerance unless every drawable account ran
        out, in which case it is short by ``drawdown_shortfall``.

    Both are summed ONLY over retired years (``any_retired`` is True), so a
    pre-retirement horizon honestly returns 0.0 -- the plan produced no
    retirement income because nobody retired, not because the drawdown failed.
    This is the after-tax retirement income / drawdown the existing machinery
    produced; this function does NOT re-derive, gross-up, or tax anything
    (DP#25: it lives in the optimization layer and depends inward on the
    simulation's outputs).

    Higher = more after-tax retirement income delivered, so the optimizer
    ranks strategies that leave the household with more spendable retirement
    income ahead. Distinct from ``max_net_benefit`` (terminal wealth) and
    ``max_after_tax_estate`` (legacy at death): a strategy can rank well here
    and poorly on estate if it spends the portfolio down to fund retirement.
    """
    total = 0.0
    for r in results:
        if r.any_retired:
            total += r.after_tax_income + r.drawdown_net_delivered
    return total


# Tolerance for "the plan met its target this year." Shortfall fields are
# floating-point dollar gaps; a sub-cent residual is rounding, not a shortfall.
_SUCCESS_TOL = 1e-6


def _probability_success(results: List[YearResult], cfg: Dict) -> float:
    """Per-path success indicator: 1.0 if the plan met every declared target
    across the horizon, 0.0 otherwise.

    Issue #728 (DP#22): "probability the plan meets its targets." A plan year
    FAILS its targets if either shortfall fired:

      - ``drawdown_shortfall`` (> 0): the retirement drawdown could not fund
        the declared ``drawdown_net_target`` spending need because every
        drawable account was exhausted (issue #707). A year with no drawdown
        requested (``drawdown_net_target`` ≈ 0) has ``drawdown_shortfall`` ≈ 0
        and is NOT a failure -- the household declared no spending need that
        year.
      - ``solvency_shortfall`` (> 0): the cash-flow identity
        ``after_tax_income + drawdown_net_delivered >= debt_service +
        living_costs + contributions`` was breached (issue #679). A year the
        solvency rule did not run (no ``household_budget.annual_living_costs``
        declared -- DP#16) has ``solvency_shortfall`` ≈ 0 and is NOT a failure.

    The plan SUCCEEDS iff NO year failed. This is a per-path binary -- the
    deterministic GridOptimizer scores a scenario 1.0 or 0.0, and
    MonteCarloOptimizer aggregates the N per-path indicators (via
    ``ObjectiveFunction.aggregate`` -> ``rank_from_distribution``) into the
    fraction of paths that succeeded, i.e. P(the plan meets its targets) under
    the return model's distribution. Reproducible per DP#23 because the MC
    seeds are reproducible (monte_carlo_optimizer.py ``seed_base + idx``).

    A household that declared NO targets (no spending need, no living-cost
    budget) trivially succeeds on every path -> P(success) = 1.0 for every
    strategy. That is the honest answer: the absence of a target is not a
    target the plan can miss (DP#32), and ranking such a household by
    ``max_probability_success`` will tie every strategy at 1.0 -- which is
    exactly what "you declared no targets" means. Use ``max_after_tax_income``
    or ``max_net_benefit`` to rank a target-less household.
    """
    for r in results:
        if r.drawdown_shortfall > _SUCCESS_TOL or r.solvency_shortfall > _SUCCESS_TOL:
            return 0.0
    return 1.0


def _p_success_from_distribution(scores: List[float], cfg: Dict) -> float:
    """Aggregate per-path success indicators into P(plan meets targets).

    Issue #728 (DP#22/DP#29): ``_probability_success`` returns 1.0 / 0.0 per
    Monte Carlo path; averaging those across N paths yields the probability
    the plan meets its targets under the return model. This is the
    ``rank_from_distribution`` MonteCarloOptimizer calls instead of the
    default ``mean(net_benefit)`` so a Monte Carlo run ranks strategies by
    P(success) rather than by expected terminal wealth.
    """
    if not scores:
        return 0.0
    return sum(1.0 for s in scores if s > 0.5) / len(scores)


# ── Risk objectives (issue #937, DP#22/DP#29) ───────────────────────────────
#
# A risk objective ranks strategies by a statistic of the *left tail* of the
# terminal-wealth distribution over a stochastic-return ensemble, not by the
# single deterministic outcome. The per-path ``fn`` is ``_terminal_wealth`` (the
# terminal net worth of one path); ``rank_from_distribution`` collapses the N
# per-path terminal net worths into the ranking scalar. Because both statistics
# are monotone in the wealth of a bad case, MAXIMIZING them prefers the strategy
# whose downside is least bad -- sequence-of-returns-risk aversion.
#
# Degenerate-by-design: a household that declared no stochastic return model has
# a one-point distribution, so CVaR and the 10th percentile both collapse to the
# single deterministic terminal net worth (DP#32: the absence of a distribution
# is not a zero distribution -- it is the honest answer "you gave me no spread").

def _cvar(scores: List[float], alpha: float) -> float:
    """Conditional Value at Risk at level ``alpha``: the MEAN of the worst
    ``alpha`` fraction of outcomes (the left tail).

    For terminal-wealth scores (higher = better) the worst outcomes are the
    SMALLEST values, so ``_cvar(scores, 0.10)`` averages the lowest-decile
    paths -- "the expected outcome in a bad case." The tail size is
    ``ceil(alpha * N)`` and is floored at 1, so a single-path distribution
    returns that one value (the degenerate case above).
    """
    if not scores:
        return 0.0
    ordered = sorted(scores)
    k = max(1, math.ceil(alpha * len(ordered)))
    tail = ordered[:k]
    return sum(tail) / len(tail)


def _low_percentile(scores: List[float], q: float) -> float:
    """The ``q``-quantile of ``scores`` by the nearest-rank method (q in [0, 1]).

    ``_low_percentile(scores, 0.10)`` is the 10th-percentile terminal net worth
    -- the threshold one path in ten falls below. Nearest-rank (no
    interpolation) keeps the returned value an ACTUAL realised path outcome,
    not a synthetic average of two paths. A single-path distribution returns
    that one value.
    """
    if not scores:
        return 0.0
    ordered = sorted(scores)
    idx = max(0, math.ceil(q * len(ordered)) - 1)
    return ordered[idx]


def _cvar10_terminal(scores: List[float], cfg: Dict) -> float:
    """rank_from_distribution for ``max_cvar_terminal``: CVaR@10% of the
    per-path terminal net worths."""
    return _cvar(scores, 0.10)


def _p10_terminal(scores: List[float], cfg: Dict) -> float:
    """rank_from_distribution for ``max_p10_terminal``: the 10th-percentile
    terminal net worth across the ensemble."""
    return _low_percentile(scores, 0.10)


# ── Pre-built objective instances ──────────────────────────────────────────

MAX_NET_BENEFIT = ObjectiveFunction(
    name="max_net_benefit",
    fn=_net_benefit,
    description=(
        "Maximize after-tax net benefit (assets - debt + tax savings - an "
        "ESTIMATED pre-death withdrawal tax). Issue #672: this estimate never "
        "models death, so it has EXACTLY ZERO sensitivity to the estate "
        "election levers (spousal rollover, TFSA successor holder, PR "
        "designation, rollover_overrides, life insurance) -- measured by "
        "#661's VOI sweep. Use max_after_tax_estate to rank by what actually "
        "reaches heirs (#580)."
    ),
)

MAX_TERMINAL_WEALTH = ObjectiveFunction(
    name="max_terminal_wealth",
    fn=_terminal_wealth,
    description=(
        "Maximize total PRE-TAX assets minus debt at end of projection. "
        "NOT an estate value: a dollar of RRSP is counted the same as a "
        "dollar of TFSA, with no deemed-disposition tax deducted. Use "
        "max_after_tax_estate to rank by what actually reaches heirs (#580)."
    ),
)

MAX_TAX_SAVINGS = ObjectiveFunction(
    name="max_tax_savings",
    fn=_total_tax_savings,
    description="Maximize total tax savings (RRSP + SM deductions)",
)

MIN_RETIREMENT_GAP = ObjectiveFunction(
    name="min_retirement_gap",
    fn=_retirement_gap,
    description="Minimize gap between wealth and retirement target (higher = more surplus)",
)

MIN_YEARS_TO_MORTGAGE_FREE = ObjectiveFunction(
    name="min_years_to_mortgage_free",
    fn=_years_to_mortgage_free,
    description="Minimize years to pay off mortgage (higher score = fewer years)",
)

MAX_SM_SAVINGS = ObjectiveFunction(
    name="max_sm_savings",
    fn=_readvance_tax_savings_total,
    description="Maximize Smith Manoeuvre tax savings",
)

MAX_AFTER_TAX_ESTATE = ObjectiveFunction(
    name="max_after_tax_estate",
    fn=_after_tax_estate,
    description=(
        "Maximize AFTER-TAX estate value (issue #580): RRSP/RRIF/LIF/LIRA "
        "taxed as ordinary income at deemed disposition (ITA s.70(5)/"
        "s.146(8.8)), TFSA and principal-residence equity pass tax-free, "
        "non-reg gains taxed at 50% inclusion on the accrued (FMV-ACB) "
        "portion only. The recommended objective for ranking strategies by "
        "legacy/estate outcome -- see compute_after_tax_estate() for the "
        "estate-input defaults this uses (issue #600 Track C: the schema "
        "has no beneficiary/rollover/successor-holder fields yet)."
    ),
)

def _neg_after_tax_estate(results: List[YearResult], cfg: Dict) -> float:
    """The die-with-zero score for ``min_after_tax_estate`` (issues #1009,
    #1065, #1081), under the ObjectiveFunction.evaluate contract ("higher is
    better"):

        score = -drawable_after_tax - debts - insolvency

    where ``drawable_after_tax`` is ``EstateResult``'s named spend-down
    surface -- every estate pot EXCEPT the designated principal residence,
    after its own deemed-disposition tax (= ``net_estate + debts -
    house_equity``) -- and ``debts``/``insolvency`` are the named terminal-
    debt and #1065 insolvency fields. Reuses ``compute_after_tax_estate`` --
    no second spelling of the death-tax math (DP#3/DP#9) -- and no second
    spelling of the decomposition either: every term is read off a NAMED
    ``EstateResult`` field, not magic arithmetic.

    Issue #1081 -- why the score is NOT ``-net_estate``: ``net_estate`` nets
    debt against assets dollar-for-dollar, so under a ``-net_estate`` score a
    strategy that BORROWS and does not repay buys exactly one point of score
    per borrowed dollar while total assets stay flat. "Minimise the estate"
    is not "die with zero": die-with-zero means ASSETS are consumed, and
    converting assets into debt must never improve the score. The fix is
    structural, not a patched branch:

      - The score ranks the SPEND-DOWN surface (``drawable_after_tax``), not
        the balance-sheet residual. The residence is outside it -- consumed
        by living in it, so KEEP-the-home is not failing to spend down; a
        sold principal already contributes neither value nor debt (#956/#964)
        and drops out naturally.
      - Debt enters ONLY as a penalty (coefficient -1 via ``- debts``), never
        as a netting term against assets. For any two otherwise-identical
        trajectories, the one with more terminal debt scores strictly lower
        -- borrowing buys zero score, with the gradient preserved (a $1 debt
        is distinguishable from a $1M debt; the #850 trap forbids collapsing
        them to a sentinel).
      - Insolvency is priced AGAIN on top (``- insolvency``, the #1065 term):
        dying owing $X stays strictly worse than dying solvent with $X left
        unspent, and clean $0 (of the spend-down surface) remains the unique
        best achievable score at 0.0 for a residence-free balance sheet.
        Without this term an insolvent death would tie the equivalent
        unspent surplus.

    Disclosed consequence (not hidden): because the residence is outside the
    spend-down surface, a death holding residence equity scores ABOVE 0.0 by
    exactly that equity -- 0.0 is the unique best only among residence-free
    balance sheets. This is correct within the optimizer's use (DP#22:
    ranking STRATEGIES for the SAME household, whose residence is fixed or
    explicitly sold by the strategy being ranked -- that sale is precisely
    reproduction (2) of #1081, where KEEP-home now outranks SELL-and-die-
    owing). It also means ``min_after_tax_estate`` and
    ``max_after_tax_estate`` are exact mirrors ONLY on the debt-free,
    residence-free slice of balance sheets; wherever debt or residence
    equity exists, the min objective deliberately diverges from the negated
    estate (that divergence IS the fix).

    Residual crossover (DISCLOSED, carried over from #1065): on the
    residence-free slice, an insolvency of $X still ties a solvent unspent
    surplus of $2X and outranks any larger surplus (``-2X`` vs ``-S``).
    Pinned by ``tests/test_issue_1065_*.py::TestInsolvencyCrossover`` so a
    future switch to infeasibility semantics is a deliberate, visible
    decision.
    """
    estate = compute_after_tax_estate(results, cfg)
    # Every term is a NAMED EstateResult field (DP#32: no magic arithmetic):
    #   - drawable_after_tax: the spend-down surface (all pots but the
    #     residence, after tax) -- what "die with zero" asks to consume;
    #   - debts: terminal non-mortgage debt, priced dollar-for-dollar as a
    #     PURE penalty -- never netted against assets (the #1081 inversion);
    #   - insolvency: the #1065 depth-of-insolvency penalty, kept so dying
    #     owing $X stays strictly worse than leaving $X unspent.
    return (-estate.drawable_after_tax
            - estate.debts
            - estate.insolvency)

MIN_AFTER_TAX_ESTATE = ObjectiveFunction(
    name="min_after_tax_estate",
    fn=_neg_after_tax_estate,
    description=(
        "DIE WITH ZERO (issues #1009/#1065/#1081): rank strategies toward "
        "consuming the household's SPEND-DOWN savings by death. Scores "
        "-(terminal drawable assets after deemed-disposition tax) with "
        "terminal debt priced dollar-for-dollar as a PURE penalty and "
        "terminal insolvency priced again on top -- debt is NEVER netted "
        "against assets, so borrowing and not repaying buys zero score. The "
        "designated principal residence sits OUTSIDE the spend-down target "
        "(it is consumed by living in it); a strategy that sells it (#956/#964) "
        "moves its proceeds into the spend-down pots naturally. Uses the SAME "
        "deemed-disposition estate math (compute_after_tax_estate) -- no "
        "death tax is re-spelled; see _neg_after_tax_estate for the exact "
        "decomposition and its disclosed properties. Pair with "
        "retirement.liquidate_to_target so the drawdown actually liquidates "
        "residual drawable financial savings to meet the target before a "
        "shortfall is reported; a max-sustainable-spend / earliest-feasible-"
        "retirement SOLVER that returns the frontier directly is a follow-up "
        "(with the liquidate fix the frontier is already discoverable by "
        "sweeping assumptions.retirement.spending_target and reading "
        "first_shortfall_year)."
    ),
)

MAX_FAMILY_AFTER_TAX_NETWORTH = ObjectiveFunction(
    name="max_family_after_tax_networth",
    fn=_family_after_tax_networth,
    description=(
        "Maximize the WHOLE FAMILY's after-tax net worth / estate at the "
        "horizon (epic #841 bite 4, DP#22): ONE number across ALL members. "
        "Sums the household's after-tax estate (both adults -- reusing "
        "max_after_tax_estate's deemed-disposition handling unchanged: RRSP/"
        "RRIF/LIF/LIRA taxed as ordinary income at death, TFSA and designated "
        "principal residence tax-free, non-reg gains at the capital-gains "
        "inclusion, spousal rollover and all debt) PLUS each CHILD's OWN "
        "after-tax net worth (their TFSA/FHSA tax-free, RRSP deemed-disposed, "
        "non-reg gains taxed) -- the child accounts (bite 2) that "
        "total_assets() deliberately excludes from the household view. Invents "
        "no new tax: every member is valued on the SAME jurisdiction "
        "deemed-disposition primitives. Equals max_after_tax_estate exactly "
        "for a household with no child-savers (the golden household), so it is "
        "opt-in and diverges only when a strategy grows a child's wealth."
    ),
)

MAX_AFTER_TAX_INCOME = ObjectiveFunction(
    name="max_after_tax_income",
    fn=_after_tax_income,
    description=(
        "Maximize total AFTER-TAX retirement income delivered across the "
        "projection (issue #728, DP#22). Sums ``after_tax_income + "
        "drawdown_net_delivered`` over retired years only (``any_retired``), "
        "consuming the retirement drawdown machinery's net (after-tax) outputs "
        "read-only -- it does NOT re-derive or re-tax the drawdown. A "
        "pre-retirement horizon returns 0.0 (nobody retired, not a drawdown "
        "failure). Distinct from max_net_benefit (terminal wealth) and "
        "max_after_tax_estate (legacy): a strategy that spends the portfolio "
        "down to fund retirement ranks well here and lower on estate."
    ),
)

MAX_PROBABILITY_SUCCESS = ObjectiveFunction(
    name="max_probability_success",
    fn=_probability_success,
    description=(
        "Maximize the probability the plan meets its targets (issue #728, "
        "DP#22/DP#29). Per-path score is 1.0 if NO year had a "
        "``drawdown_shortfall`` (retirement spending unmet, #707) or "
        "``solvency_shortfall`` (cash-flow identity breached, #679), else "
        "0.0. Deterministic (single-path) optimizers score a scenario 1.0 / "
        "0.0; MonteCarloOptimizer aggregates the N per-path indicators into "
        "P(success) under the return model (reproducible seeds, DP#23). A "
        "household that declared no targets trivially succeeds on every path "
        "(P=1.0 for all strategies) -- the honest answer to 'you declared no "
        "targets'; rank such a household by max_after_tax_income or "
        "max_net_benefit instead."
    ),
    rank_from_distribution=_p_success_from_distribution,
)

MAX_CVAR_TERMINAL = ObjectiveFunction(
    name="max_cvar_terminal",
    fn=_terminal_wealth,
    description=(
        "Maximize CVaR@10% of terminal net worth -- the MEAN terminal net "
        "worth of the worst 10% of return paths (issue #937, DP#22). A risk "
        "objective: it ranks a strategy by how bad its BAD case is, not by its "
        "expected outcome, so it prefers the strategy whose downside tail is "
        "least deep (sequence-of-returns-risk aversion). Meaningful only over a "
        "stochastic return ensemble -- the optimizer runs one when this "
        "objective is selected AND the household declared a stochastic return "
        "model (assumptions.return_model type=stochastic/mean_reverting). With "
        "no declared spread the distribution is one point and CVaR collapses to "
        "the deterministic terminal net worth (DP#32). Compare max_terminal_"
        "wealth (expected value) and max_p10_terminal (the 10th-percentile "
        "threshold rather than the tail mean)."
    ),
    rank_from_distribution=_cvar10_terminal,
)

MAX_P10_TERMINAL = ObjectiveFunction(
    name="max_p10_terminal",
    fn=_terminal_wealth,
    description=(
        "Maximize the 10th-percentile terminal net worth -- the threshold one "
        "return path in ten falls below (issue #937, DP#22). A risk objective: "
        "it lifts the floor of the outcome distribution. Differs from "
        "max_cvar_terminal in what it measures of the tail: P10 is the "
        "boundary of the worst decile, CVaR is the average DEPTH within it, so "
        "CVaR is the more conservative of the two when the tail is fat. Runs "
        "over the same stochastic ensemble and degenerates to the deterministic "
        "terminal net worth when the household declared no return spread."
    ),
    rank_from_distribution=_p10_terminal,
)

# Registry of all built-in objectives
OBJECTIVES = {
    'max_net_benefit': MAX_NET_BENEFIT,
    'max_terminal_wealth': MAX_TERMINAL_WEALTH,
    'max_tax_savings': MAX_TAX_SAVINGS,
    'min_retirement_gap': MIN_RETIREMENT_GAP,
    'min_years_to_mortgage_free': MIN_YEARS_TO_MORTGAGE_FREE,
    'max_sm_savings': MAX_SM_SAVINGS,
    'max_after_tax_estate': MAX_AFTER_TAX_ESTATE,
    'min_after_tax_estate': MIN_AFTER_TAX_ESTATE,
    'max_family_after_tax_networth': MAX_FAMILY_AFTER_TAX_NETWORTH,
    'max_after_tax_income': MAX_AFTER_TAX_INCOME,
    'max_probability_success': MAX_PROBABILITY_SUCCESS,
    'max_cvar_terminal': MAX_CVAR_TERMINAL,
    'max_p10_terminal': MAX_P10_TERMINAL,
}


def get_objective(name: str) -> ObjectiveFunction:
    """Look up a built-in objective by name."""
    if name not in OBJECTIVES:
        raise ValueError(f"Unknown objective: {name}. Available: {list(OBJECTIVES.keys())}")
    return OBJECTIVES[name]