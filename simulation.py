#!/usr/bin/env python3
"""
Simulation Engine — Year-by-year financial projection.

This is the core engine that takes a strategy, family state, rate path,
and produces year-by-year projection results.

Composable: swap in different strategies, rate paths, or tax calculators
without changing the engine.

DP#8: Compose through data, not through inheritance. The simulation engine
receives a JurisdictionAdapter that provides jurisdiction-specific factories.
DP#25: If code compiles with only the root package on PYTHONPATH, it is
jurisdiction-agnostic by construction. Importing from a jurisdiction package
is a deliberate act; core should never require it.

References:
    ITA Part I — income tax computation, deduction rules
    CRA T4040 — RRSPs and Other Registered Plans for Retirement:
        https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html
    Year-specific limits (RRSP, TFSA, FHSA, YMPE):
        https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html

Usage:
    from simulation import FamilySimulation
    from simulation_config import SimulationConfig
    from countries.canada.adapter import CanadaAdapter  # Deliberate jurisdiction import
    
    config = SimulationConfig.from_json('input.json')
    adapter = CanadaAdapter(config)
    
    sim = FamilySimulation(config, adapter=adapter)
    results = sim.run()
    print(results[-1].net_assets)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from copy import deepcopy
from pathlib import Path

logger = logging.getLogger(__name__)

from tax_calculator import (
    marginal_rate, tax_on_income,
    effective_tax_rate, capital_gains_rate,
)
from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState, AllocationResult,
)
from jurisdiction import (
    JurisdictionAdapter, QCDeductionResult,
)

# Import config classes from the new module to break cycles
from simulation_config import SimulationConfig, YearResult, ScenarioOverlay, build_overlay_config
# Issue #681: the trajectory invariants are LIBRARY code, asserted from the
# run path itself -- not a test-only harness that a real household can walk
# straight past (see trajectory_invariants.py's module docstring).
from trajectory_invariants import assert_run_invariants, InvariantBreachedError


# =============================================================================
# DP#26/#583: the annual step is a pure function over explicit state
# =============================================================================
#
# `simulate_year` (and the rule functions it calls: `_year_brackets_for`,
# `_mortgage_data_for`, `_non_reg_after_tax_return_for`) are module-level
# functions, not methods. They take `state`, `year`, and a
# `SimulationContext` as ordinary arguments and read nothing off any
# instance -- there is no `self` in scope for them to read. The retirement
# transition that used to live here as `_retirement_transition_for` is now
# a registered rule (`retirement_income` in simulation_rules.py, epic #795
# bite 1). `FamilySimulation` (below) gathers its own
# attributes into a `SimulationContext` once per call
# (`_build_context()`) and hands it to `simulate_year`; that gathering
# step is the ONLY place these attributes are read off `self`, and it is
# not "the year step" -- it is the seam between the object and the pure
# fold. See `tests/test_issue_583_pure_fold.py` for the architecture test
# that enforces this mechanically.
#
# Per issue #583 / Barley's #575 handover: every field bundled into
# SimulationContext is per-run static configuration/composition data
# (config, strategy, rate objects, the tax provider, RESP calculator/
# children, portfolio *composition*) or a value read fresh from the
# household's CURRENT state at context-build time (`has_fhsa`). None of
# them is a *growth balance* cached once and read as if current in a
# later year -- that was the actual #575 bug (`self._portfolio.balance`),
# and evolving balances always arrive as the explicit `state` argument to
# `simulate_year`, never through this context.

@dataclass(frozen=True)
class SimulationContext:
    """Everything `simulate_year` needs, besides `state` and `year` (DP#26/#583)."""
    config: SimulationConfig
    strategy: AllocationStrategy
    rate_path: object
    heloc_path: object
    use_readvanceable: bool
    deduct_later: bool
    lump_sum: float
    # Issue #914: non-borrowed year-0 cash (RESP-collapse/EAP proceeds booked as
    # property.free_cash by apply_overlay). Distinct from lump_sum -- it is NOT
    # borrowed, so it adds no HELOC debt and no s.20(1)(c) deductibility.
    free_cash: float
    return_model: object
    tax_provider: object
    amort_annual: list
    amort: list
    resp_calc: object
    resp_children: list
    has_fhsa: bool
    frozen_brackets: bool
    brackets: list
    start_year: int
    primary_income: float   # base (year-0) primary gross income
    spouse_income: float    # base (year-0) spouse gross income
    portfolio: object       # non-reg composition (DP#8: static input data)


def _year_brackets_for(sim_year: int, *, frozen_brackets: bool, brackets: list,
                        tax_provider, province: str, start_year: int) -> list:
    """Tax brackets for a simulation year, with warning on fallback (DP#20/#26)."""
    if frozen_brackets:
        return brackets
    try:
        return tax_provider.get_combined_brackets(
            year=sim_year, province=province)
    except ValueError:
        import warnings
        warnings.warn(
            f"No tax bracket data for {sim_year}, using {start_year} "
            f"brackets. Set frozen_brackets=True for intentional isolation.")
        return brackets


def _mortgage_data_for(year: int, *, amort_annual: list, amort: list) -> Dict:
    """Extract mortgage data from amortization for a given year (DP#26)."""
    for ann in amort_annual:
        if ann['year'] == year + 1:
            return ann

    # Fallback
    months = [m for m in amort if m['year'] == year + 1]
    if months:
        return {
            'year': year + 1,
            'total_payment': sum(m['payment'] for m in months),
            'total_interest': sum(m['interest'] for m in months),
            'total_principal': sum(m['principal'] for m in months),
            'total_readvanced': sum(m['principal'] for m in months),
            'end_balance': months[-1]['balance'],
            'heloc_end': months[-1].get('heloc_balance', 0),
        }
    return {
        'year': year + 1, 'total_payment': 0, 'total_interest': 0,
        'total_principal': 0, 'total_readvanced': 0,
        'end_balance': 0, 'heloc_end': 0,
    }


def _non_reg_after_tax_return_for(year: int, primary_marginal_rate: float,
                                   gross_return: float, *, portfolio,
                                   non_reg_yield_rate: float, province: str) -> float:
    """DP#27: One model of taxable investing, for non-reg AND (via the same
    ``non_reg_after_tax_return`` value threaded into ``simulate_year_pure``)
    Smith-Manoeuvre investments (#576) -- both are non-registered/taxable
    accounts by construction and must compound under the same physics.

    #575/DP#32: this is a **pure function of composition and the marginal
    rate** -- never of any account's *current balance*. A non-reg balance
    of $0 is the ordinary starting condition for a new taxable investor
    (or for a Smith-Manoeuvre household before its first readvance), not
    a reason to report a 0% rate. This function never reads a ``.balance``
    off ``portfolio`` at all -- only its *composition*, which is static
    input config (DP#8), not derived simulation state.

    #576: the old formula compounded only the account's *declared* yield
    (interest/dividends) after tax and silently discarded ``gross_return``
    entirely -- capital appreciation, the dominant term for a low-turnover
    equity portfolio, was never applied. The declared yield is taxed
    annually per income type (DP#27); the remainder of the total return
    (``gross_return`` minus the declared yield) is genuine capital
    appreciation that accrues untaxed as an unrealized gain -- it does not
    touch ACB (DP#19: cost basis grows only with contributions) and is
    taxed only when realized, via the FMV-vs-ACB gain fraction already
    computed in ``plan_drawdown_net``.

    Args:
        year: Simulation year (0-based). Composition is static input data
            today, so this is currently unused; kept for a future
            year-varying composition without changing the call signature.
        primary_marginal_rate: Primary earner's marginal rate for tax calc.
        gross_return: Gross investment return for this year (ReturnModel).
        portfolio: Static portfolio composition (or None).
        non_reg_yield_rate: Configurable fallback declared-yield rate
            (DP#13) when no explicit non-reg composition is configured.
        province: Province for after-tax rate computation.

    Returns:
        After-tax return rate for non-reg / SM-investment growth. Never
        ``None`` -- absent an explicit ``portfolio.accounts.non_reg`` block,
        this falls back to ``non_reg_yield_rate`` as the assumed
        declared-yield composition (DP#13: a configurable default, not a
        hardcoded rate, and not "no tax at all").
    """
    non_reg_acct = None
    if portfolio is not None and portfolio.has_data:
        non_reg_acct = portfolio.accounts.get('non_reg')

    if non_reg_acct is None:
        # No non-reg account in portfolio config — use a configurable
        # default composition (DP#2/DP#13), not a hardcoded rate.
        from countries.canada.portfolio import AccountPortfolio, YieldBreakdown
        non_reg_acct = AccountPortfolio(
            yield_breakdown=YieldBreakdown(interest=non_reg_yield_rate),
        )

    declared_yield = non_reg_acct.yield_breakdown.total_yield
    after_tax_yield = non_reg_acct.after_tax_return_by_account(
        'non_reg', primary_marginal_rate, province)
    # #576: capital appreciation -- the part of the total return that is
    # not a declared distribution -- is deferred (untaxed) growth.
    capital_appreciation = gross_return - declared_yield

    return after_tax_yield + capital_appreciation


def _registered_wht_drag_for(portfolio) -> Optional[Dict[str, float]]:
    """Issue #641: the per-registered-pot WHT drag for this run's portfolio, or
    ``None`` when nothing is declared.

    Generalizes the ``non_reg`` composition->growth wiring to the registered
    pots: just as ``_non_reg_after_tax_return_for`` reads ``portfolio.accounts.
    non_reg`` to derive a growth rate at the impure boundary and threads a
    scalar into the pure fold, this reads ``portfolio.accounts.{rrsp,tfsa}`` to
    derive each pot's foreign-withholding-tax drag (the only tax that leaks from
    a sheltered account) and threads the ``{kind: rate}`` map in. ``None`` for
    an absent/empty portfolio keeps the flat gross rate (golden no-op, DP#32).
    """
    if portfolio is None:
        return None
    drag = portfolio.registered_wht_drag()
    return drag or None


# ── Issue #674: dated income segments + earned-income-vs-taxable-income ────
#
# The dated income-segment blending below is GENERIC income arithmetic (epic
# #795 bite 3): it derives a year's income from stored ``[from, to)`` windows by
# day count, the way ``age_in(year)`` derives age from a birth date (DP#1), so
# it stays here as an intentionally-core fold primitive. The ONE program-shaped
# fragment -- the ITA s.146(1) earned-income classification that decides which
# ``kind`` accrues RRSP room -- is Canadian tax law and now lives in the Canada
# jurisdiction module (DP#10/DP#25). ``_income_components_for_year`` LAZY-imports
# ``is_earned_income`` from there (DP#25: no jurisdiction import at simulation.py
# module scope) and routes each segment's ``kind`` through it. See
# countries/canada/earned_income.py.


def _self_employment_net_amount(seg: Dict) -> float:
    """Issue #980 (T2125): the NET business income of ONE self_employment
    income segment = gross fees less deductible professional expenses.

    A self-employed professional's tax base is NET business income, not gross
    fees: gross minus the T2125 expenses (home-office business-use-%,
    professional dues + mandatory liability insurance, professional
    development, meals/entertainment at 50% inclusion). That net -- NOT the
    gross -- feeds tax, the self-employed contribution stack (#978 QPP both
    halves + QPIP + individual HSF), and RRSP-room accrual (ITA s.146(1)
    "earned income" is net self-employment income, not gross).

    ``expenses_annual`` is the single-scalar T2125 total the schema declares
    on a self_employment income segment ($defs/income.expenses_annual /
    $defs/income_override.expenses_annual) -- one spelling of the 'net =
    gross - expenses' fact, the SAME shape the structurally identical rental
    case uses (``property[kind=rental].expenses_annual``, CRA T776), not a
    per-category block the engine merely sums (DP#2: the per-category
    breakdown is the user's record-keeping, not engine logic; DP#7: model
    the net-income mechanism, not the branded T2125 form's line items; DP#9:
    one spelling of the same fact, not two). The user enters meals already at
    the 50% inclusion the CRA admits, since the engine models the net total,
    not per-category inclusion.

    DP#32 absence-safe: a segment with NO ``expenses_annual`` key (or null)
    has zero expenses, so net == gross -- byte-identical to pre-#980. The
    explicit ``is None`` test (not ``or 0.0``) keeps a legitimately-declared
    ``0`` expenses value honest (DP#32: zero is a value, not a fallback;
    ``or`` would be mechanically flagged by the architecture guard and would
    conflate 'absent' with 'present and zero' -- though both yield 0 here, the
    guard cannot know that, and the explicit form documents the intent).
    A negative net is left AS-IS: a deductible loss carried against other
    income is the user's T2125 reality, not an engine opinion (a self-
    employment loss IS deductible; flooring it at zero would silently drop a
    real deduction -- the 'returning 0 on <= 0' trap the codebase has fallen
    into before).

    Pure function (DP#3): same inputs → same output. Called by BOTH
    ``_income_components_for_year`` (tax base + earned income / RRSP room)
    and ``_self_employment_income_for_year`` (contribution-stack base), so
    all three consumers of a self_employment segment read the SAME net --
    one spelling, not three (DP#9).
    """
    amount = seg["amount"]
    expenses = seg.get("expenses_annual")
    if expenses is None:
        return amount
    return amount - expenses


def _income_components_for_year(base_amount: float, segments: Optional[List[Dict]],
                                 calendar_year: int, salary_growth: float,
                                 year_index: int) -> Tuple[float, float]:
    """Issue #674 (Gap 1 + Gap 2): ``(total_income, earned_income)`` for ONE
    family member for ONE calendar year.

    ``segments`` is the member's ``income_segments`` -- a list of dated
    overrides (``{'kind', 'amount', 'from', 'to'}``, ``to=None`` meaning
    open-ended) attached by ``optimize.py``'s ``_apply_income_scenario``.
    DP#1: the source of truth is the stored ``[from, to)`` window, never a
    flat whole-horizon number -- this function derives the year's income
    from those dates every time it's called, the same way ``age_in(year)``
    is derived from a birth date.

    For whatever part of the calendar year no segment covers, the base
    salary-grown employment income applies (``base_amount * (1 +
    salary_growth) ** year_index`` -- the engine's pre-#674 behaviour,
    unchanged when no segments are declared at all). The two are blended by
    DAY COUNT, so a mid-year shock (job loss March 1, back to work November
    1) is not rounded up to a full year of EI, nor a full year of lost room
    -- exactly the coarsening DP#1 warns against.

    ``total_income`` is what tax/cash-flow reads (EI IS taxable income).
    ``earned_income`` is what RRSP-room accrual reads -- only the fraction
    of the year covered by an ``EARNED_INCOME_KINDS`` segment, or by the
    base income (always employment), counts.

    Issue #980 (T2125): a ``self_employment`` segment contributes its NET
    business income (gross fees - ``expenses_annual``) to BOTH
    ``total_income`` and ``earned_income``, via
    :func:`_self_employment_net_amount` -- the same net the #978
    contribution-stack base reads -- so tax, RRSP room, and the QC stack
    all see the net, not the gross (one spelling, DP#9). A segment with no
    ``expenses_annual`` contributes its gross amount unchanged
    (byte-identical to pre-#980, DP#32 absence-safe).

    Declared segments must not overlap -- silently using one and dropping
    the other is exactly the "returning the first match" trap this codebase
    has fallen into before (AGENTS.md); it raises ``ValueError`` instead.
    """
    from datetime import date
    # DP#25: the ITA s.146(1) earned-income classification is Canadian tax law;
    # lazy-import it (no jurisdiction import at simulation.py module scope).
    from countries.canada.earned_income import is_earned_income

    year_start = date(calendar_year, 1, 1)
    year_end = date(calendar_year + 1, 1, 1)
    days_in_year = (year_end - year_start).days

    parsed = []
    for seg in (segments or []):
        seg_from = date.fromisoformat(seg["from"])
        seg_to = date.fromisoformat(seg["to"]) if seg.get("to") else None
        # Issue #980 (T2125): a self_employment segment's tax base and RRSP-
        # room base are its NET business income (gross - expenses_annual),
        # not its gross fees. Thread the segment through the net helper so
        # both total_income (tax) and earned_income (RRSP room) read the net.
        # Other kinds have no expenses_annual (the schema only declares it on
        # self_employment), so the helper returns their gross amount unchanged.
        net_amount = (_self_employment_net_amount(seg)
                      if seg.get("kind") == "self_employment" else seg["amount"])
        parsed.append((seg_from, seg_to, seg["kind"], net_amount))
    parsed.sort(key=lambda s: s[0])

    for i in range(1, len(parsed)):
        prev_from, prev_to, _, _ = parsed[i - 1]
        cur_from, _, _, _ = parsed[i]
        if prev_to is None or prev_to > cur_from:
            raise ValueError(
                f"income_segments overlap: a segment starting {prev_from} "
                f"({'open-ended' if prev_to is None else f'ending {prev_to}'}) "
                f"overlaps the segment starting {cur_from}. Each income_id "
                f"can only be in one state at a time -- fix the declared "
                f"from/to dates (issue #674)."
            )

    total_income = 0.0
    earned_income = 0.0
    covered_days = 0
    for seg_from, seg_to, kind, amount in parsed:
        overlap_start = max(seg_from, year_start)
        overlap_end = min(seg_to, year_end) if seg_to is not None else year_end
        if overlap_end <= overlap_start:
            continue
        days = (overlap_end - overlap_start).days
        covered_days += days
        fraction = days / days_in_year
        total_income += amount * fraction
        if is_earned_income(kind):
            earned_income += amount * fraction

    remaining_days = days_in_year - covered_days
    if remaining_days > 0:
        base_this_year = base_amount * (1 + salary_growth) ** year_index
        fraction = remaining_days / days_in_year
        total_income += base_this_year * fraction
        earned_income += base_this_year * fraction  # base income is employment

    return total_income, earned_income


def _self_employment_income_for_year(base_amount: float, segments: Optional[List[Dict]],
                                      calendar_year: int, salary_growth: float,
                                      year_index: int) -> float:
    """Issue #978: the year's SELF-EMPLOYMENT income for ONE family member.

    The fold's tax path already reads the year's total income via
    :func:`_income_components_for_year`; this companion extracts the slice of
    it that came from ``income_segments`` whose ``kind == 'self_employment'``
    (DP#1: the source of truth is the stored dated window, never a flat whole-
    horizon flag). That slice is the base for the mandatory Quebec self-employed
    contribution stack (QPP both halves + QPIP self-employed + the individual
    Health Services Fund) the working-phase cash flow must charge (#978).

    Issue #980 (T2125): the base is the segment's NET business income (gross
    fees - ``expenses_annual``), via :func:`_self_employment_net_amount` --
    the SAME net the tax path and RRSP-room accrual read -- so the stack is a
    percentage of NET, not gross (expenses reduce the QPP/QPIP/HSF base too).
    One spelling of the net, not three (DP#9).

    Day-blended the SAME way ``_income_components_for_year`` blends total/
    earned income, so a mid-year transition (employee Jan-Aug, self-employed
    Sep-Dec) charges the stack on only the self-employed days, not a full year
    -- exactly the coarsening DP#1 warns against. The base salary-grown income
    is EMPLOYMENT income (the engine's pre-#674 default), so it contributes 0
    to this slice: a member with no ``kind == 'self_employment'`` segment has
    $0 self-employment income, and the contribution stack is a strict no-op for
    them (DP#32 absence-safe -- a missing input does not silently invent a
    premium; it produces zero, the value an employee's stack is).

    Pure function (DP#3): same inputs → same output.
    """
    from datetime import date

    year_start = date(calendar_year, 1, 1)
    year_end = date(calendar_year + 1, 1, 1)
    days_in_year = (year_end - year_start).days

    self_emp_income = 0.0
    for seg in (segments or []):
        if seg.get("kind") != "self_employment":
            continue
        seg_from = date.fromisoformat(seg["from"])
        seg_to = date.fromisoformat(seg["to"]) if seg.get("to") else None
        overlap_start = max(seg_from, year_start)
        overlap_end = min(seg_to, year_end) if seg_to is not None else year_end
        if overlap_end <= overlap_start:
            continue
        days = (overlap_end - overlap_start).days
        fraction = days / days_in_year
        # Issue #980 (T2125): the contribution-stack base is NET business
        # income (gross fees - expenses_annual), the SAME net the tax path
        # and RRSP-room accrual read via _self_employment_net_amount above --
        # one spelling, not three (DP#9). Expenses reduce the QPP/QPIP/HSF
        # base too: the stack is a percentage of net, not gross.
        self_emp_income += _self_employment_net_amount(seg) * fraction

    # The base salary-grown income (the portion of the year no segment covers)
    # is employment income -- it contributes 0 to self-employment income.
    return self_emp_income


def _self_employed_contribution_stack(
        self_employment_income: float, province: str, sim_year: int,
) -> float:
    """Issue #978: the mandatory Quebec self-employed contribution stack on
    net business income, as a single pre-savings cash outflow.

    A self-employed Quebec earner owes -- on their net business income -- the
    contributions an employer would have split with an employee, plus the
    individual Health Services Fund (QPP both halves + QPIP self-employed +
    the individual HSF up to $1,000). The fold's working-phase solvency
    identity must charge this stack or it over-states the earner's disposable
    income (and thus savings capacity) by the full stack vs an employee at
    the same gross (#978). The calculators ALL EXIST and are correct; the
    assembler lives in ``countries.canada.self_employed_contributions`` (a
    jurisdiction module, where the ``'quebec'`` gate belongs -- DP#8/DP#10:
    province is data, not a hardcoded literal in jurisdiction-agnostic core)
    and reuses them with one spelling (DP#9 -- do NOT re-spell).

    DP#25: ``countries.canada`` is imported lazily -- no jurisdiction import
    at simulation.py module scope, and no ``'quebec'`` literal in core
    (issue #241). DP#32: a member with NO self-employment income owes the full
    stack on $0, which the assembler returns as 0.0, so the stack is
    byte-for-byte a no-op for an employee or a member with no self-employment
    segment -- the golden household (employment income only) is unchanged.
    """
    # DP#25 / issue #241: lazy-import the jurisdiction assembler so core
    # never spells 'quebec' itself.
    from countries.canada.self_employed_contributions import (
        self_employed_contribution_stack,
    )
    return self_employed_contribution_stack(self_employment_income, province, sim_year)


def _income_shock_active_for_year(base_amount: float, segments: Optional[List[Dict]],
                                  calendar_year: int, salary_growth: float,
                                  year_index: int) -> bool:
    """Issue #761: True when a dated ``income_segments`` override ACTIVE this
    calendar year reduces this member's income BELOW the no-override baseline
    -- i.e. a job-loss / salary-cut shock, the scenario ``decisions.income[]``
    exists to model.

    The detection is *behavioural*, not structural: a segment whose ``kind`` is
    ``ei`` or whose ``amount`` is below the grown base salary reads as a
    reduction; a ``p1_promotion`` override (amount ABOVE baseline) does NOT,
    because a raise is not a shock that forces a household to cut discretionary
    spending. Concretely, ``_income_components_for_year`` blends the segment
    amount over the covered days and the grown base over the rest, so the
    year's total is below the full-year baseline exactly when an active
    segment's amount is below the grown base for the covered fraction -- which
    is the precise definition of "income reduced by an override this year."

    A year with no segments at all (the baseline / no-shock scenario) returns
    False -- discretionary compression never fires without a dated shock, so a
    contract that declares a split but runs a no-shock scenario reproduces
    today's numbers byte-for-byte (the DP#32/no-op property #761 requires).
    """
    if not segments:
        return False
    baseline_full_year = base_amount * (1 + salary_growth) ** year_index
    actual, _ = _income_components_for_year(
        base_amount, segments, calendar_year, salary_growth, year_index)
    return actual < baseline_full_year - 1e-6


def _private_loan_interest_for(
        cfg, sim_year: int, primary_member: dict, spouse_member: dict
) -> Tuple[float, float, float, float]:
    """Issue #832: the per-member taxable-income adjustments a private
    loan produces for calendar ``sim_year`` -- computed ONCE for both
    time-steps (DP#9: one spelling, not two).

    A private loan is a loan FROM AN INDIVIDUAL. The lender is either a
    declared household member (a person_id) or an individual OUTSIDE the
    household (an inline identity, ``lender_is_external``). The year's
    interest is ``rate x principal`` -- but it produces a TAX flow ONLY when
    interest is actually PAID/PAYABLE this year (``interest == 'paid'`` or
    ``repayment == 'amortizing'``, which implies paid). When interest is NOT
    payable (the default on_demand/on_demand demand loan), there is NO tax
    flow at all: no lender income, no borrower deduction -- the loan is
    modeled as interest-free financing (free liquidity), not a forced 5%
    deductible/taxable split. ITA s.20(1)(c) interest is deductible only when
    paid or payable, and it is income to the lender only when received/
    receivable.

    When interest IS paid/payable, two effects apply (both to TAXABLE INCOME
    before ``tax_on_income``, reusing the existing s.20(1)(c) deductible-
    interest trace, not duplicating it):

    - LENDER accrues taxable interest INCOME -- but ONLY when the lender is a
      declared household member (an external individual is not a simulated
      member, so their tax is outside this engine's scope, like a bank's).
      Added to the lender's taxable income so it is taxed in their own
      bracket. When the lender is a MINOR (age < 18 at sim_year, derived from
      birth_year -- DP#1), ITA s.74.2 attributes that interest back to the
      BORROWER instead. An ADULT lender (18+) is exempt from attribution.

    - BORROWER's interest is DEDUCTIBLE (subtracted from taxable income) only
      when ``use == 'investment'`` (ITA s.20(1)(c): the proceeds earn income).
      ``use == 'consumption'`` -> no deduction (personal interest is not
      deductible). This applies whether the lender is internal or external.

    Only the two TAXED members (primary/spouse) have a tax computation in this
    engine (#701: children are not taxed as individuals). The common,
    fully-taxed case is a loan between the two spouses (or from an external
    individual to a spouse).

    Returns ``(primary_income_adj, spouse_income_adj, primary_deduction,
    spouse_deduction)`` -- the income to ADD to each taxed member's taxable
    income and the deduction to SUBTRACT. All zero when no private loans are
    declared, OR when every declared loan is on-demand with no interest
    demanded (the golden household, and the #832 motivating demand loan, are
    both unaffected).
    """
    loans = getattr(cfg, 'private_loans', [])
    if not loans:
        return 0.0, 0.0, 0.0, 0.0

    # Epic #795 bite 4 (DP#10/DP#25): the ITA tax law -- s.20(1)(c) interest
    # payability/deductibility and s.74.2 minor-lender attribution -- lives in
    # the Canada jurisdiction module and is lazy-imported here. What remains in
    # the generic fold is only the household-structure PLUMBING: resolve each
    # person_id to a taxed role, read ages from birth_year (DP#1), decide
    # external-vs-internal from the lender's SHAPE, accumulate per role, and
    # surface the two #701/#832 contradiction warnings loudly (DP#32).
    from countries.canada.private_loan_interest import classify_private_loan_interest

    primary_id = primary_member.get('id', '')
    spouse_id = spouse_member.get('id', '')
    children_by_id = {c.get('id'): c for c in cfg.children if c.get('id')}

    def _age(person_id: str) -> Optional[int]:
        for m in (primary_member, spouse_member):
            if m.get('id') == person_id and m.get('birth_year'):
                return sim_year - int(m['birth_year'])
        ch = children_by_id.get(person_id)
        if ch and ch.get('birth_year'):
            return sim_year - int(ch['birth_year'])
        return None

    # Map a person_id to ('primary'|'spouse'|None) -- only taxed members can
    # receive an income addition or a deduction here.
    def _taxed_member_of(person_id: str) -> Optional[str]:
        if person_id == primary_id:
            return 'primary'
        if person_id == spouse_id:
            return 'spouse'
        return None

    p_income = 0.0      # interest income added to the primary's taxable income
    s_income = 0.0      # ... spouse
    p_deduction = 0.0   # deductible interest subtracted from the primary's income
    s_deduction = 0.0   # ... spouse

    for loan in loans:
        lender = loan['lender']
        borrower_id = loan['borrower']
        # The lender's SHAPE is authoritative (DP#32): a dict is an inline
        # external individual (outside the household -- not a simulated member,
        # so untaxed here, like a bank), a string is a household person_id. A
        # loan dict built directly (e.g. in a test) is handled the same way.
        lender_is_external = isinstance(lender, dict)
        lender_age = None if lender_is_external else _age(lender)
        lender_role = None if lender_is_external else _taxed_member_of(lender)

        effect = classify_private_loan_interest(
            loan,
            lender_is_external=lender_is_external,
            lender_age=lender_age,
            lender_role=lender_role,
            borrower_role=_taxed_member_of(borrower_id),
            borrower_is_child=borrower_id in children_by_id,
        )

        # Accumulate the classified interest into the two taxed roles. The
        # lender-side income lands on the lender (or, under s.74.2, the borrower)
        # and the s.20(1)(c) deduction on the borrower -- each reusing the same
        # income adjustment as RRSP/SM deductions, not a new mechanism.
        if effect.income_role == 'primary':
            p_income += effect.interest
        elif effect.income_role == 'spouse':
            s_income += effect.interest
        if effect.deduction_role == 'primary':
            p_deduction += effect.interest
        elif effect.deduction_role == 'spouse':
            s_deduction += effect.interest

        # The two #701 contradictions the engine cannot fully model: surface
        # them loudly rather than dropping them silently (DP#32).
        if effect.warn_adult_child_lender_untaxed:
            logger.warning(
                "CONTRADICTION (#832, DP#32): private loan %r lender %r is an "
                "ADULT child (age %d) -- attribution does NOT apply (18+ "
                "exempt), so the interest income ($%.0f) is the child's to "
                "tax in their own low bracket. But this engine does not tax "
                "children as individuals (#701), so the interest is EARNED "
                "but NOT TAXED here. Do not believe the split was modelled "
                "when it was not; track a per-child tax bracket to close this.",
                loan.get('id'), lender, lender_age, effect.interest)
        if effect.warn_child_borrower_deduction_unusable:
            logger.warning(
                "CONTRADICTION (#832, DP#32): private loan %r borrower %r is a "
                "CHILD with use=investment -- the interest IS deductible under "
                "s.20(1)(c), but this engine does not tax children (#701), so "
                "the deduction has no tax to reduce. Recorded, not applied; "
                "track a per-child tax bracket to close this.",
                loan.get('id'), borrower_id)

    return p_income, s_income, p_deduction, s_deduction


def _rental_income_for(
        cfg, sim_year: int, primary_member: dict, spouse_member: dict,
        opening_ucc_by_prop: Dict[str, float]
) -> Tuple[float, float, float, float, float, float, Dict[str, float]]:
    """Issue #693 (epic #690 bite 2): the per-taxed-member rental-income
    adjustments a declared rental property produces for ``sim_year`` -- computed
    ONCE for both time-steps (DP#9: one spelling, not two).

    A ``kind=rental`` property the couple owns produces NET RENTAL INCOME (gross
    rent less operating expenses, CRA form T776), ordinary income taxable at the
    owner's marginal rate, and the mortgage interest on the debt secured against
    it is DEDUCTIBLE under ITA s.20(1)(c) (the property earns income -- unlike a
    RRSP/TFSA loan, which s.18(11) makes non-deductible). This is the same
    (income, deduction) shape a private loan already produces (issue #813), so it
    rides the SAME taxable-income adjustment: the operating income is added to,
    and the deductible interest subtracted from, the owner's taxable income
    before ``tax_on_income`` -- reusing the existing s.20(1)(c) deductible-
    interest trace, not a new mechanism.

    Epic #690 bite 2 (DP#10/DP#25): the ITA/T776 tax law -- net rental income and
    the s.20(1)(c) interest deduction -- lives in the Canada jurisdiction module
    (``countries.canada.rental_income``, lazy-imported here). What remains in the
    generic fold is only the household-structure PLUMBING: read each property's
    declared facts off the internal config and split the whole-property figures
    by the couple's declared OWNER->ROLE shares (mapped in
    ``input_contract._map_owned_properties``).

    Returns ``(primary_operating, spouse_operating, primary_interest_deduction,
    spouse_interest_deduction, primary_cca, spouse_cca, new_ucc_by_prop)`` --
    each role's share of the operating income (gross - expenses) to ADD to
    taxable income and of the deductible mortgage interest to SUBTRACT. The
    role's NET rental income (operating - deduction) is both taxed AND real
    household cash (unlike an intra-household loan, the rent is paid by an
    outside tenant), so the caller adds it to after-tax income too. All zero when
    no couple-owned rental property is declared (the golden household -- a no-op,
    DP#32).

    Issue #694 (epic #690 bite 3): a rental that also declares a CCA election
    depreciates its building against the rental income -- a NON-CASH deduction
    (ITA s.20(1)(a)) that lowers TAXABLE income but not cash. The claim is capped
    at the property's net rental income before CCA (it cannot create or deepen a
    loss) and depreciates a declining-balance UCC tracked in
    ``opening_ucc_by_prop`` (read from ``jurisdiction_state['canada']``
    ``['rental_ucc']``; the declared opening UCC on the first year the property
    has no carried balance). ``primary_cca``/``spouse_cca`` are the year's CCA to
    SUBTRACT from each owner's TAXABLE income ONLY (NOT after-tax cash -- CCA is
    non-cash); ``new_ucc_by_prop`` is the closing UCC per property the caller
    threads to next year. The recapture of this CCA lands at the estate (#694).
    The last three are inert (0.0 / {}) when no property declares a CCA election.
    """
    props = getattr(cfg, 'properties', [])
    if not props:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, {}

    from countries.canada.rental_income import classify_rental_income
    from countries.canada.cca import cca_claim, ucc_after_claim

    p_op = s_op = p_ded = s_ded = 0.0
    p_cca = s_cca = 0.0
    new_ucc_by_prop: Dict[str, float] = {}
    for prop in props:
        # Issue #696 (epic #690 bite 5): a property bought mid-horizon does not
        # exist -- and so earns no rent and claims no CCA -- before its purchase
        # year. A property held from year 0 has no `purchase`, so this is a no-op
        # (byte-identical to #693/#694, DP#32).
        purchase = prop.get('purchase')
        if purchase is not None and sim_year < purchase['year']:
            continue
        rental = prop.get('rental')
        if not rental:
            continue  # a cottage/vacant land carries equity only, no rental income
        # Issue #967: a financed rental's mortgage interest is DEDUCTIBLE
        # under s.20(1)(c), but the mortgage ORIGINATES at the purchase year
        # -- the static `mortgage_interest_annual` (built from year-0
        # secured_liabs) is $0 for a financed rental. The per-year interest
        # lives on the `financing_schedule` carried by the rental block (a
        # reference to the financing block's precomputed amortization
        # schedule). Add the dynamic interest for `sim_year` to the deduction
        # so a rental bought with a mortgage deducts its serviced interest
        # each year (ITA s.20(1)(c)). A rental with no `financing_schedule`
        # (held from year 0, or equity-financed) reads the static
        # `mortgage_interest_annual` only, byte-identical to #693 (DP#32). The
        # schedule covers origination-to-payoff; a year outside the schedule
        # (before purchase, or after payoff) contributes 0 -- the before-
        # purchase gate above already skipped pre-purchase years, and after
        # payoff the interest is genuinely 0.
        mortgage_interest = rental['mortgage_interest_annual']
        fin_schedule = rental.get('financing_schedule')
        if fin_schedule:
            for entry in fin_schedule:
                if entry['year'] == sim_year:
                    mortgage_interest += entry['interest']
                    break
        effect = classify_rental_income(
            rental['gross_rent_annual'],
            rental['expenses_annual'],
            mortgage_interest)
        roles = rental['owner_roles']
        p_frac = roles.get('primary', 0.0)
        s_frac = roles.get('spouse', 0.0)
        p_op += effect.operating_income * p_frac
        s_op += effect.operating_income * s_frac
        p_ded += effect.deductible_interest * p_frac
        s_ded += effect.deductible_interest * s_frac
        # Issue #694: Capital Cost Allowance, if this property elected it. The
        # claim is capped at net rental income BEFORE CCA (op - deductible
        # interest, effect.net_rental_income) so it never creates a loss, and
        # depreciates a declining UCC. The half-year rule is an acquisition-year
        # concern (a mid-projection purchase is #696); a rental declared with a
        # running UCC is an EXISTING property, so is_acquisition_year stays False.
        cca = rental.get('cca')
        if cca is not None:
            prop_id = prop['id']
            opening = opening_ucc_by_prop.get(prop_id, cca['opening_ucc'])
            claim = cca_claim(opening, cca['rate'], effect.net_rental_income,
                              is_acquisition_year=False)
            new_ucc_by_prop[prop_id] = ucc_after_claim(opening, claim)
            # Split the whole-property claim to each taxed owner in the same
            # owner_roles proportion the operating income is split, so each
            # role's CCA deduction offsets the income it earned.
            p_cca += claim * p_frac
            s_cca += claim * s_frac
    return p_op, s_op, p_ded, s_ded, p_cca, s_cca, new_ucc_by_prop


def _short_term_rental_facts(cfg) -> Tuple[float, bool]:
    """Issue #697 (epic #690 bite 6): the household's SHORT-TERM-rental (Airbnb)
    reporting facts for a year -- the total net BUSINESS income earned by declared
    STR properties, and whether any of them crosses the $30k GST/HST small-supplier
    threshold (ETA s.148).

    A ``kind=rental`` property whose ``rental`` block carries a ``short_term``
    marker is an STR (already legality-gated at load by
    ``input_contract._map_owned_properties``; a banned/unregistered STR never
    reaches here). Its net income is ACTIVE business income (ITA s.9), computed by
    the SAME net-income arithmetic as a long-term rental and so ALREADY inside
    ``net_rental_income`` -- this surfaces the STR SUBSET distinctly, it does not
    add income twice. Returns ``(str_business_income, gst_hst_registration_required)``
    -- ``(0.0, False)`` when no STR is declared (the golden path -- a no-op, and a
    long-term rental never sets the flag, DP#32). The tax law lives in the Canada
    module (``countries.canada.short_term_rental``, lazy-imported); the fold only
    reads the declared facts and accumulates (DP#25)."""
    props = getattr(cfg, 'properties', [])
    if not props:
        return 0.0, False
    from countries.canada.short_term_rental import classify_str_income
    str_income = 0.0
    registration_required = False
    for prop in props:
        rental = prop.get('rental')
        if not rental:
            continue
        short_term = rental.get('short_term')
        if not short_term:
            continue  # a long-term rental: property income, no GST/HST supply
        effect = classify_str_income(
            rental['gross_rent_annual'],
            rental['expenses_annual'],
            rental['mortgage_interest_annual'])
        str_income += effect.net_business_income
        registration_required = (
            registration_required or effect.gst_hst_registration_required)
    return str_income, registration_required


def _income_tax_by_adult(config, income_by_role, loan_by_role, brackets):
    """Issue #701 (Step 5 of #643): tax each adult individually via a loop.

    Canada has no joint filing, so each adult's marginal rate and pre-credit
    tax (``tax_on_income``) fall on that adult's OWN, private-loan-interest-
    adjusted income (issue #813). Looping over ``config.adults()`` reproduces
    the former hardcoded primary/spouse pair EXACTLY for a two-adult household
    and generalizes to N adults in one place.

    ``simulate_year_pure`` keeps its two-slot (primary/spouse) signature this
    step, so both slots are always populated: a role the household does not
    declare (e.g. no spouse -- ``config.adults()`` omits it) is backfilled from
    its own zero-income slot, keeping that retained interface byte-identical to
    the prior code (``marginal_rate``/``tax_on_income`` are non-zero at $0 of
    income only for the rate, so the backfill -- not a dropped slot -- is what
    preserves behaviour). Returns ``{role: {'rate', 'taxable_income',
    'tax_before'}}``.
    """
    def _slot(role):
        income = income_by_role[role]
        loan_inc, loan_ded = loan_by_role[role]
        taxable = income + loan_inc - loan_ded
        return {
            'rate': marginal_rate(income, brackets),
            'taxable_income': taxable,
            'tax_before': tax_on_income(taxable, brackets),
        }

    result = {adult['role']: _slot(adult['role']) for adult in config.adults()}
    # Backfill the roles the retained two-slot signature still consumes.
    for role in ('primary', 'spouse'):
        if role not in result:
            result[role] = _slot(role)
    return result


def _extra_adult_specs(cfg, sim_year: int, salary_growth: float, year: int,
                       year_brackets) -> list:
    """Issue #899 (part a): for each ADDITIONAL accumulating adult (the adults
    beyond the primary couple -- ``config.adults()[2:]``), the year's income,
    its earned-income component (RRSP-room base), the individually-computed tax
    (Canada has no joint filing), and the OWN after-tax savings available to
    invest this year.

    Empty for a two-adult household, so every caller's downstream loop is a
    strict no-op there -- the byte-identical guarantee for the golden invariant.
    Uses the SAME dated-income mechanism (`_income_components_for_year`) and the
    SAME year-versioned brackets the primary couple's tax uses (DP#9 -- one
    income/tax spelling for every member). These adults never retire across the
    horizon (guaranteed by input_contract's admission gate), so their income is
    always the pre-retirement grown employment figure -- no drawdown path.
    """
    from tax_calculator import tax_on_income
    specs = []
    for adult in cfg.adults()[2:]:
        income, earned = _income_components_for_year(
            adult.get('gross_income', 0.0), adult.get('income_segments'),
            sim_year, salary_growth, year)
        tax = tax_on_income(income, year_brackets)
        specs.append({
            'id': adult.get('id', adult.get('role')),
            'role': adult.get('role'),
            'income': income,
            'earned_income': earned,
            'tax': tax,
            'savings': (income - tax) * cfg.savings_rate,
        })
    return specs


def _adult_income_maps(primary_income, spouse_income, p_loans, s_loans,
                       extra_specs):
    """Issue #899 (part a): the ``income_by_role`` / ``loan_by_role`` maps the
    per-adult tax loop (:func:`_income_tax_by_adult`) consumes, extended with
    the additional accumulating adults' income (no private-loan interest is
    modelled for an extra adult, so their loan entry is a hard zero). For a
    two-adult household ``extra_specs`` is empty, so these are exactly the
    former primary/spouse-only maps -- byte-identical."""
    income_by_role = {'primary': primary_income, 'spouse': spouse_income}
    loan_by_role = {'primary': p_loans, 'spouse': s_loans}
    for s in extra_specs:
        income_by_role[s['role']] = s['income']
        loan_by_role[s['role']] = (0.0, 0.0)
    return income_by_role, loan_by_role


def _prior_gis_countable(results) -> Optional[float]:
    """Issue #1020 (S04 Step 1): the prior year's GIS-countable income.

    GIS uses CRA's PRIOR-YEAR income test. The countable base is the prior
    year's TOTAL net income EXCLUDING OAS (OAS is excluded from the GIS
    income test by statute -- see ``countries.canada.retirement.gis_benefit``'s
    ``net_income`` contract: "excluding OAS, but including CPP, RRIF, etc.").
    That includes EMPLOYMENT income in a pre-retirement / part-retirement
    prior year (a still-working spouse's salary IS countable), so the base is
    the prior year's ``retirement_income`` (CPP + OAS + pension + GIS +
    drawdown + LIF) PLUS ``employment_income``, MINUS OAS and GIS (both
    excluded from the test). CRA-faithful: the year before 65 is a working
    year, its salary is countable, and that is exactly why the preservation
    maneuver must be set up in the 50s.

    Returns None when there is no prior year (year 0), so the
    ``retirement_income`` rule leaves GIS at its seeded 0.0 (DP#32: absence is
    a loud no-op, never a silent zero-coercion).
    """
    if not results:
        return None
    prior = results[-1]
    # Countable = everything taxable the household received, minus OAS and
    # GIS (both non-countable for the GIS test). employment_income is the
    # post-retirement-stop employment sum (0 once fully retired); retirement_
    # income carries CPP/pension/drawdown/LIF; both together span the whole
    # net-income base the CRA test sees.
    return (prior.retirement_income
            + prior.employment_income
            - prior.oas_income
            - getattr(prior, 'gis_income', 0.0))


def simulate_year(state, year: int, ctx: SimulationContext,
                  prior_gis_countable_income=None) -> Tuple[YearResult, 'object']:
    """Pure annual step (DP#26/#583): ``(state, year, ctx) -> (YearResult, next_state)``.

    This function reads NOTHING off any instance. ``state`` is the fold's
    current ``SimState``, ``year`` is the 0-based projection index, and
    ``ctx`` bundles the run's static config/strategy/rate/provider objects
    (see ``SimulationContext`` and ``FamilySimulation._build_context()``).
    Folding this over ``range(projection_years)`` reproduces ``run()``
    exactly; this function never mutates ``state`` or ``ctx``.

    Issue #1020 (S04 Step 1): ``prior_gis_countable_income`` is the prior
    year's GIS-countable income (retirement income excluding OAS), threaded
    from the prior ``YearResult`` by the fold. None (year 0 / no prior year)
    -> the ``retirement_income`` rule leaves GIS at its seeded 0.0 (DP#32).
    """
    from simulation_state import (
        simulate_year_pure, child_savings_for_year, child_gift_funding_for_year,
        child_loan_funded_for_year,
        child_after_tax_savings_for_year,
        adult_rrsp_slot, adult_tfsa_slot,
        adult_fhsa_total_room, adult_fhsa_total_lifetime_remaining)

    cfg = ctx.config
    salary_growth = cfg.salary_growth
    sim_year = ctx.start_year + year

    # ── Compute incomes for this year (issue #674: dated income_segments,
    # not just the base salary-grown flat amount) ──
    primary_member = next((m for m in cfg.family_members if m.get('role') == 'primary'), {})
    spouse_member = next((m for m in cfg.family_members if m.get('role') == 'spouse'), {})
    primary_income, primary_earned_income = _income_components_for_year(
        ctx.primary_income, primary_member.get('income_segments'),
        sim_year, salary_growth, year)
    spouse_income, spouse_earned_income = _income_components_for_year(
        ctx.spouse_income, spouse_member.get('income_segments'),
        sim_year, salary_growth, year)
    # Issue #978: each member's SELF-EMPLOYMENT income slice for the year --
    # the base for the mandatory Quebec self-employed contribution stack
    # (QPP both halves + QPIP self-employed + individual HSF) the working-phase
    # cash flow charges below. $0 for a member with no `kind == 'self_employment'`
    # segment (DP#32 absence-safe), so the golden household (employment only) is
    # a strict no-op.
    primary_self_emp = _self_employment_income_for_year(
        ctx.primary_income, primary_member.get('income_segments'),
        sim_year, salary_growth, year)
    spouse_self_emp = _self_employment_income_for_year(
        ctx.spouse_income, spouse_member.get('income_segments'),
        sim_year, salary_growth, year)
    # Issue #761: a working-life year whose income is reduced below baseline
    # by a dated decisions.income[] shock -- compresses the discretionary
    # portion of living_costs in apply_solvency when a split is declared.
    income_shock_active = (
        _income_shock_active_for_year(
            ctx.primary_income, primary_member.get('income_segments'),
            sim_year, salary_growth, year)
        or _income_shock_active_for_year(
            ctx.spouse_income, spouse_member.get('income_segments'),
            sim_year, salary_growth, year))

    # ── Issue #294 / epic #795 bite 1: retirement transition ──
    # Once a member reaches retirement_age, stop their employment income
    # (no salary, no further salary growth) so the allocation strategy and
    # the marginal-rate / after-tax / savings math below see the RETIRED
    # income figure. The CPP/OAS/pension + drawdown-sizing half of the
    # transition is now a REGISTERED RULE (``retirement_income`` in
    # simulation_rules.py) that runs inside simulate_year_pure and writes
    # its outputs to YearWorkingState; the prologue passes only the
    # retirement STATUS (a date-computed eligibility gate, DP#1/#28) and
    # the pre-retirement grown incomes + brackets + indexation rate the
    # rule needs. DP#25: countries.canada imported lazily.
    from countries.canada.retirement_transition import (
        is_retired, DEFAULT_RETIREMENT_AGE,
    )
    p_retired = is_retired(
        primary_member.get('birth_year', 0),
        primary_member.get('retirement_age', DEFAULT_RETIREMENT_AGE),
        sim_year) if primary_member else False
    s_retired = is_retired(
        spouse_member.get('birth_year', 0),
        spouse_member.get('retirement_age', DEFAULT_RETIREMENT_AGE),
        sim_year) if spouse_member else False
    # Keep the grown pre-retirement incomes for the retirement_income rule
    # (it zeroes them itself for the covered_net shortfall math).
    primary_income_pre, spouse_income_pre = primary_income, spouse_income
    if p_retired:
        primary_income = 0.0
        primary_earned_income = 0.0
        # Issue #978: the stack is on WORKING self-employment income -- a
        # retired member earns no self-employment salary, so charge no stack.
        primary_self_emp = 0.0
    if s_retired:
        spouse_income = 0.0
        spouse_earned_income = 0.0
        spouse_self_emp = 0.0

    total_income = primary_income + spouse_income
    for ch in cfg.children:
        if ch.get('gross_income', 0) > 0:
            total_income += ch['gross_income'] * (1 + salary_growth) ** year

    annual_savings = total_income * cfg.savings_rate

    # DP#18: apply CashFlow events
    for cf in cfg.cash_flows:
        if cf.get('year', 0) == sim_year:
            cf_amount = cf.get('amount', 0)
            cf_tax = cf.get('tax_treatment', 'post-tax')
            if cf_tax == 'pre-tax':
                annual_savings += cf_amount
            elif cf_tax == 'non-taxable':
                annual_savings += cf_amount
            else:
                annual_savings += cf_amount

    # ── Marginal rates (year-specific brackets per DP#20) ──
    year_brackets = _year_brackets_for(
        sim_year, frozen_brackets=ctx.frozen_brackets, brackets=ctx.brackets,
        tax_provider=ctx.tax_provider, province=cfg.province, start_year=ctx.start_year)

    # Issue #813: private loan interest -- the lender accrues taxable
    # interest income (in their bracket) and the borrower's interest is
    # deductible under s.20(1)(c) only when use=investment. Applied to TAXABLE
    # INCOME here (before tax_on_income), reusing the same income-reduction
    # trace as RRSP/Smith deductions -- NOT a new deduction mechanism. All
    # zero when no private loans are declared (the golden household). Minor-
    # lender attribution (s.74.2) is handled inside the helper.
    _p_loan_inc, _s_loan_inc, _p_loan_ded, _s_loan_ded = _private_loan_interest_for(
        cfg, sim_year, primary_member, spouse_member)

    # Issue #693 (epic #690 bite 2): a declared rental property's net rental
    # income (gross rent - operating expenses, T776) is ordinary income for the
    # owner, and the mortgage interest on it is deductible under s.20(1)(c).
    # Same (income, deduction) shape as a private loan, so it rides the SAME
    # taxable-income adjustment below (op income added, interest subtracted).
    # All zero when no couple-owned rental is declared (the golden household).
    # Issue #694 (bite 3): a declared CCA election also depreciates the building
    # against rental income (a non-cash deduction lowering taxable income) and
    # tracks a declining UCC threaded through jurisdiction_state['canada']
    # ['rental_ucc']. _p_rent_cca/_s_rent_cca and the closing UCC are inert
    # (0.0 / {}) when no property elects CCA.
    (_p_rent_op, _s_rent_op, _p_rent_ded, _s_rent_ded,
     _p_rent_cca, _s_rent_cca, _rental_ucc) = _rental_income_for(
        cfg, sim_year, primary_member, spouse_member,
        state.jurisdiction_state.get('canada', {}).get('rental_ucc', {}))

    # Issue #701 (Step 5 of #643): tax each adult individually via a loop over
    # config.adults() -- each adult's marginal rate and pre-credit tax on their
    # OWN loan-interest-adjusted income (Canada has no joint filing). Isomorphic
    # to the former hardcoded primary/spouse pair for the two-adult household;
    # simulate_year_pure keeps its two-slot signature this step, so the
    # primary/spouse scalars it still consumes are read back by role here.
    # Issue #899 (part a): the additional accumulating adults' income/tax, so
    # the per-adult tax loop taxes each of them individually (and the OWN
    # after-tax savings that fund their OWN accounts below). Empty for a
    # two-adult household -> the maps and the accumulation are byte-identical.
    # Issue #693: the rental operating income and its s.20(1)(c) interest
    # deduction join the SAME per-role taxable-income adjustment as the private
    # loan (income added, deduction subtracted -- one spelling, DP#9).
    _extra_specs = _extra_adult_specs(cfg, sim_year, salary_growth, year, year_brackets)
    # Issue #694: CCA is an additional NON-CASH deduction against rental income,
    # so it joins the interest deduction in the TAXABLE-income adjustment (income
    # added, deduction + CCA subtracted) but -- unlike the interest -- it is NOT
    # subtracted from after-tax cash below (see _after_tax_by_role): depreciation
    # lowers the tax bill without consuming cash.
    _income_by_role, _loan_by_role = _adult_income_maps(
        primary_income, spouse_income,
        (_p_loan_inc + _p_rent_op, _p_loan_ded + _p_rent_ded + _p_rent_cca),
        (_s_loan_inc + _s_rent_op, _s_loan_ded + _s_rent_ded + _s_rent_cca),
        _extra_specs)
    _adult_tax = _income_tax_by_adult(cfg, _income_by_role, _loan_by_role, year_brackets)
    primary_rate = _adult_tax['primary']['rate']
    spouse_rate = _adult_tax['spouse']['rate']

    # Issue #679: after-tax employment income for the cash-flow solvency
    # identity (simulation_rules.apply_solvency). Actual combined tax on
    # each earner's own income (tax_on_income), not a flat marginal-rate
    # approximation -- each earner is taxed separately (Canada has no joint
    # filing), same per-person split the RRSP-deduction rules already use
    # above/below. Approximation this shares with the rest of the engine's
    # tax modeling: CPP/EI payroll premiums are not separately deducted
    # (no employee-premium model exists anywhere else in this engine
    # either). Epic #795 bite 3: each taxed member's OWN federal (+ QC
    # provincial) tuition credit (#764, non-refundable, #784 carry-forward,
    # #785 transfers) is no longer applied here -- it is a REGISTERED RULE
    # (``tuition_credit`` in simulation_rules.py) that runs inside
    # simulate_year_pure and writes the per-member tax reduction to
    # YearWorkingState; apply_solvency adds it to `available`. The prologue
    # passes the PRE-credit tax_before (below) so the rule can apply the
    # credit; after_tax_income below is therefore PRE-credit, and the rule
    # + apply_solvency restore the POST-credit figure on YearResult.
    primary_tax_before = _adult_tax['primary']['tax_before']
    spouse_tax_before = _adult_tax['spouse']['tax_before']
    # Issue #956 bite B (sale-core): each taxed member's TAXABLE INCOME base
    # (the dollars the pre-credit tax above is computed on), passed so the
    # registered property_disposition rule can band a sold property's capital
    # gain against the owner's actual taxable income (the marginal rate the
    # gain lands in), reusing estate.tax_on_capital_gain_at_death's
    # ``other_income`` argument exactly as the terminal-estate path does
    # (DP#9). 0.0 for a no-income member.
    primary_taxable_income = _adult_tax['primary']['taxable_income']
    spouse_taxable_income = _adult_tax['spouse']['taxable_income']
    # Issue #813: the family-loan interest is an INTRA-HOUSEHOLD transfer
    # (borrower pays lender), so it NETS TO ZERO in household cash income -- only
    # its TAX effects remain (lender taxed on the interest, borrower gets a
    # deduction when use=investment). The taxes above are computed on the
    # interest-adjusted taxable income, but after_tax_income stays on BASE
    # income so the transfer does not invent or destroy household cash.
    # Issue #701 (Step 5): summed per adult over config.adults() -- byte-
    # identical to the former primary+spouse sum for the two-adult household.
    # Issue #693: a rental's NET rental income (operating income - deductible
    # interest) is REAL household cash -- the rent is paid by an outside tenant,
    # not an intra-household transfer -- so unlike the loan interest above it IS
    # added to after-tax income (the tax on it is already in primary/spouse_tax,
    # computed on the rental-adjusted taxable income). Zero for a household with
    # no rental (the golden path), so after_tax_income is unchanged (DP#32).
    # Epic #795 bite 3: PRE-credit tax (tax_before) -- the tuition_credit rule
    # inside simulate_year_pure applies the credit and apply_solvency adds the
    # reduction back, so YearResult.after_tax_income is the POST-credit figure.
    # Issue #978: the mandatory Quebec self-employed contribution stack
    # (QPP both halves + QPIP self-employed + individual HSF) is a pre-savings
    # cash OUTFLOW on net business income the bracket-only tax path never
    # charged -- subtracting it from each earner's after-tax income makes the
    # solvency identity see the real disposable income and savings capacity.
    # $0 for a member with no self-employment income (the calculators floor at
    # 0), so an employee or the golden household is byte-for-byte unchanged.
    primary_contrib_stack = _self_employed_contribution_stack(
        primary_self_emp, cfg.province, sim_year)
    spouse_contrib_stack = _self_employed_contribution_stack(
        spouse_self_emp, cfg.province, sim_year)
    _after_tax_by_role = {
        'primary': primary_income + (_p_rent_op - _p_rent_ded) - primary_tax_before
        - primary_contrib_stack,
        'spouse': spouse_income + (_s_rent_op - _s_rent_ded) - spouse_tax_before
        - spouse_contrib_stack,
    }
    # Issue #899 (part a): the household solvency figure is the PRIMARY couple's
    # after-tax income only. An additional accumulating adult is a separate
    # economic unit whose own after-tax income funds its OWN accounts (below,
    # mirroring the child-saver model), so it must not subsidise the primary
    # household's cash-flow identity. For a two-adult household adults() is
    # exactly the couple, so this sum is byte-identical to the pre-#899 one.
    after_tax_income = sum(
        _after_tax_by_role[adult['role']] for adult in cfg.adults()
        if adult['role'] in ('primary', 'spouse'))

    # ── Epic #841 bite 2 / issue #812: a child's OWN income funds a child's
    # OWN accounts. Carve each child's OWN savings out of the household
    # allocation base so the same child dollars that used to be routed into the
    # ADULT pot now fund the child's OWN accounts instead (DP#18: redirected,
    # never created; the fold grows the child accounts in simulate_year_pure).
    # sum(child_savings) is 0 for the golden household (no child income), so
    # this leaves the adult allocation base bit-identical there.
    child_savings = child_savings_for_year(
        cfg.children, cfg.savings_rate, salary_growth, year)
    # Issue #701 Step 6: the child's income folded into total_income above is
    # GROSS, so the adult-base carve stays GROSS (fold and carve are the same
    # artifact pair -- they net to zero on the adult base, leaving it bit-
    # identical whatever the child earns). What actually LANDS in the child's
    # accounts is the child's AFTER-TAX income (the child is taxed individually
    # on their own return, in their own lower bracket) -- so gift room-capping
    # below uses the after-tax figure, the true amount the child's own income
    # contributes. tax_on_income(0)=0 keeps both figures 0 for a zero-income
    # child (incl. the golden household).
    child_savings_after_tax = child_after_tax_savings_for_year(
        cfg.children, cfg.savings_rate, salary_growth, year, year_brackets)
    # Epic #841 bite 3: parent->child gifts fund the child's registered room
    # beyond the child's own income. The gift is carved out of the ADULT base
    # too (DP#18: the parent's investable dollars are REDIRECTED to the child's
    # own registered accounts, never created) -- capped to the child's remaining
    # registered room AFTER the child's own after-tax savings take their share.
    # sum(child_gifts) is 0 for the golden household (no gifts), so the adult
    # base stays bit-identical there.
    child_gifts = child_gift_funding_for_year(
        cfg.children, getattr(cfg, 'gifts', []), child_savings_after_tax,
        state.jurisdiction_state.get('canada', {}).get('child_accounts', []))
    adult_annual_savings = annual_savings - sum(child_savings) - sum(child_gifts)
    # Issue #859 (Part A): the LOAN-kind portion of that gift funding (a subset,
    # capped to the SAME room). It does not affect the carve or the child's
    # growth (both already use the total child_gifts) -- it is threaded onto the
    # child's loan_funded_principal so the family balance sheet books the
    # lender's receivable / the child's liability (DP#18). Zero without a
    # repayable gift (the golden household).
    child_loans = child_loan_funded_for_year(
        cfg.children, getattr(cfg, 'gifts', []), child_savings_after_tax,
        state.jurisdiction_state.get('canada', {}).get('child_accounts', []))

    # ── Build FamilyState for strategy engine ──
    _canada = state.jurisdiction_state.get('canada', {})
    family_state = FamilyState(
        primary_income=primary_income,
        spouse_income=spouse_income,
        primary_marginal_rate=primary_rate,
        spouse_marginal_rate=spouse_rate,
        primary_rrsp_room=adult_rrsp_slot(_canada, 0)[1],  # #700: per-adult own_room
        spouse_rrsp_room=adult_rrsp_slot(_canada, 1)[1],
        primary_tfsa_room=adult_tfsa_slot(_canada, 0)[1],  # #700: per-adult room
        spouse_tfsa_room=adult_tfsa_slot(_canada, 1)[1],
        # Issue #893: size the household FHSA budget against the TOTAL household
        # room/lifetime so a SECOND owner's FHSA is funded (the per-owner fill
        # happens in rebuild_adult_fhsa). One owner => slot 0's values.
        fhsa_room=adult_fhsa_total_room(_canada),
        fhsa_lifetime_remaining=adult_fhsa_total_lifetime_remaining(_canada) if ctx.has_fhsa else 0,
        resp_eligible_children=sum(1 for c in ctx.resp_children if c.cesg_eligible(sim_year)),
        resp_annual_match_cap=ctx.resp_calc.cesg_contribution_max(sim_year),  # #1046: was 0, zeroed the min-term
        annual_savings=adult_annual_savings,
        bracket_gap=primary_rate - spouse_rate,
    )

    # ── Allocate savings ──
    engine = StrategyEngine(ctx.strategy)

    # Issue #679: the dollars of this year's contributions that came from
    # BORROWING (the year-0 leveraged lump sum: a HELOC margin draw + a
    # mortgage cash-out), not from the household's pay. Measured as what
    # `fill_room` ACTUALLY allocated, not as ctx.lump_sum -- a lump sum larger
    # than the available contribution room is not fully invested, and only the
    # invested part belongs in the cash-flow identity. The solvency rule adds
    # this to its `available` inflows; the debt it created is booked and
    # serviced independently, every year after this one.
    borrowed_investment = 0.0
    # Issue #850: the BORROWED lump sum's non-registered portion -- the only
    # income-producing use, hence the only part of it whose interest is
    # deductible under s.20(1)(c) (interest on money borrowed to fill an RRSP
    # or TFSA is not: the plan's income is sheltered). Distinct from the year's
    # TOTAL `non_reg` allocation below, which also carries salary-funded
    # savings that were never borrowed. 0.0 in every year but year 0, and in
    # year 0 for a household that took no lump sum (DP#32).
    lump_non_reg = 0.0

    if ctx.lump_sum > 0 and year == 0:
        # Allocate lump sum first, then annual savings. Issue #792: honour a
        # declared deductible-vs-registered advance split when the household
        # has stated one (None = today's registered-first internal optimization).
        lump_alloc = engine.fill_room(
            ctx.lump_sum, family_state,
            deductible_non_reg_first=ctx.config.refinance_advance_deductible_non_reg,
        )
        annual_alloc = engine.allocate(family_state)
        lump_non_reg = lump_alloc.non_reg
        borrowed_investment = (
            lump_alloc.primary_rrsp + lump_alloc.spousal_rrsp + lump_alloc.spouse_rrsp
            + lump_alloc.primary_tfsa + lump_alloc.spouse_tfsa
            + lump_alloc.fhsa + lump_alloc.resp + lump_alloc.non_reg
        )
        alloc = AllocationResult(
            primary_rrsp=lump_alloc.primary_rrsp + annual_alloc.primary_rrsp,
            spousal_rrsp=lump_alloc.spousal_rrsp + annual_alloc.spousal_rrsp,
            spouse_rrsp=lump_alloc.spouse_rrsp + annual_alloc.spouse_rrsp,
            primary_tfsa=lump_alloc.primary_tfsa + annual_alloc.primary_tfsa,
            spouse_tfsa=lump_alloc.spouse_tfsa + annual_alloc.spouse_tfsa,
            fhsa=lump_alloc.fhsa + annual_alloc.fhsa,
            resp=lump_alloc.resp + annual_alloc.resp,
            non_reg=lump_alloc.non_reg + annual_alloc.non_reg,
            unused=0,
        )
    else:
        alloc = engine.allocate(family_state)

    # Issue #914: the household's declared RESP action (EAP vs collapse)
    # resolves to net proceeds that arrive as year-0 FREE CASH (booked as
    # property.free_cash by apply_overlay, threaded as ctx.free_cash). Unlike
    # the leveraged lump sum it is NOT borrowed -- it adds no HELOC debt and
    # establishes no s.20(1)(c) deductibility, so it stays OUT of
    # borrowed_investment / lump_non_reg above. It is invested at year 0 into
    # the non-registered account (the one landing that never silently drops
    # dollars to a registered-room cap downstream, and whose ACB the invested
    # cash correctly establishes). free_cash_invested is the matching INFLOW
    # the solvency identity needs so the contribution it funds is not read as a
    # shortfall (the same inflow==outflow logic as borrowed_investment). 0.0 in
    # every year but year 0, and in year 0 for a household with no free cash
    # (DP#32 -- the absent path is byte-for-byte today's behaviour).
    free_cash_invested = 0.0
    if ctx.free_cash > 0 and year == 0:
        free_cash_invested = ctx.free_cash
        alloc.non_reg += ctx.free_cash

    # ── Build allocations dict for pure function ──
    allocations = {
        'primary_rrsp': alloc.primary_rrsp,
        'spousal_rrsp': alloc.spousal_rrsp,
        'spouse_rrsp': alloc.spouse_rrsp,
        'primary_tfsa': alloc.primary_tfsa,
        'spouse_tfsa': alloc.spouse_tfsa,
        'fhsa': alloc.fhsa,
        'resp': alloc.resp,
        'non_reg': alloc.non_reg,
        '_primary_income': primary_income,
        '_spouse_income': spouse_income,
        # Issue #674: RRSP room accrual (simulation_rules.apply_contribution_
        # room) reads THESE, not the taxable totals above -- an EI-kind
        # segment is taxable income but $0 of it is "earned income" under
        # ITA s.146(1).
        '_primary_earned_income': primary_earned_income,
        '_spouse_earned_income': spouse_earned_income,
        '_annual_savings': annual_savings,
        # Issue #850: what the 'borrowing_purpose' rule traces the year-0
        # borrowings to -- how much was borrowed and invested this year, and
        # how much of that reached an income-producing use. See `lump_non_reg`
        # above. Both are 0.0 in every year but year 0.
        '_lump_sum': ctx.lump_sum if year == 0 else 0.0,
        '_lump_non_reg': lump_non_reg,
    }

    # ── Compute RESP CESG/QESI grants (pre-computed, passed as data) ──
    resp_data = []
    if ctx.resp_children:
        for ch in ctx.resp_children:
            ch_contrib = alloc.resp / max(1, len(ctx.resp_children))
            rd = {'contribution': ch_contrib, 'cesg': 0.0, 'qesi': 0.0}
            if ch.cesg_eligible(sim_year):
                cesg_result = ctx.resp_calc.calculate_cesg(
                    ch_contrib, ch, sim_year, total_income)
                qesi_result = ctx.resp_calc.calculate_qesi(
                    ch_contrib, ch, sim_year, total_income)
                rd['cesg'] = cesg_result['total_cesg']
                rd['qesi'] = qesi_result['total_qesi']
                # #1046: advance lifetime state so CESG/QESI caps bind
                ch.total_cesg_received += cesg_result['total_cesg']
                ch.total_qesi_received += qesi_result['total_qesi']
                ch.total_contributions += ch_contrib
                age = sim_year - ch.birth_year
                if age <= 15:
                    ch.total_before_age_15 += ch_contrib
                ch.contribution_years.append((sim_year, ch_contrib))
            else:
                # Child not CESG-eligible this year; still track contribution
                ch.total_contributions += ch_contrib
                age = sim_year - ch.birth_year
                if age <= 15:
                    ch.total_before_age_15 += ch_contrib
                ch.contribution_years.append((sim_year, ch_contrib))
            resp_data.append(rd)

    # ── Rates for this year ──
    mortgage_rate = ctx.rate_path.get_rate(year)
    heloc_rate = ctx.heloc_path.get_heloc_rate(year, ctx.rate_path.rate_type)
    mort = _mortgage_data_for(year, amort_annual=ctx.amort_annual, amort=ctx.amort)

    # ── FHSA contribution from allocation strategy (issue #124) ──
    fhsa_contrib = alloc.fhsa if ctx.has_fhsa else 0.0

    # ── Pure simulation step (DP#26: same inputs → same outputs) ──
    # DP#20: year-specific contribution limits
    rrsp_limit = ctx.tax_provider.get_rrsp_limit(sim_year)
    tfsa_limit = ctx.tax_provider.get_tfsa_limit(sim_year)

    # DP#27: Compute income-type-specific after-tax return for non-reg
    non_reg_atr = _non_reg_after_tax_return_for(
        year, primary_rate, ctx.return_model.return_for_year(year),
        portfolio=ctx.portfolio, non_reg_yield_rate=cfg.non_reg_yield_rate,
        province=cfg.province)

    # Issue #899 (part a): grow each additional accumulating adult's OWN
    # RRSP/TFSA from their OWN after-tax savings (computed above) against the
    # opening per-adult store slots (>= 2). Empty for a two-adult household ->
    # nothing is written back inside simulate_year_pure -> byte-identical.
    from simulation_state import step_extra_adult_accounts as _step_extra_adults
    _opening_canada = state.jurisdiction_state.get('canada', {})
    extra_adult_accounts = _step_extra_adults(
        _opening_canada.get('adult_rrsp', {}), _opening_canada.get('adult_tfsa', {}),
        _extra_specs, ctx.return_model.return_for_year(year), rrsp_limit, tfsa_limit)

    result, next_state = simulate_year_pure(
        state=state,
        year=year,
        calendar_year=sim_year,  # issue #343: drives LIRA→LIF conversion at age 71
        allocations=allocations,
        config=cfg,
        investment_return=ctx.return_model.return_for_year(year),
        mortgage_rate=mortgage_rate,
        heloc_rate=heloc_rate,
        mortgage_data=mort,
        use_readvanceable=ctx.use_readvanceable,
        deduct_later=ctx.deduct_later,
        primary_marginal_rate=primary_rate,
        spouse_marginal_rate=spouse_rate,
        resp_data=resp_data if resp_data else None,
        fhsa_contribution=fhsa_contrib,
        fhsa_annual_limit=ctx.tax_provider.get_fhsa_limit(sim_year) if ctx.has_fhsa else None,
        rrsp_annual_limit=rrsp_limit,
        tfsa_annual_limit=tfsa_limit,
        non_reg_after_tax_return=non_reg_atr,
        # Issue #641: registered pots' foreign-WHT drag from their declared
        # holdings (None when no registered composition -- golden no-op).
        registered_wht_drag=_registered_wht_drag_for(ctx.portfolio),
        # epic #795 bite 1: the retirement transition OUTPUTS (cpp_income,
        # oas_income, drawdown_net_target, any_retired, ...) are no longer
        # passed by the prologue -- the registered `retirement_income` rule
        # computes them inside simulate_year_pure and writes them to
        # YearWorkingState. The prologue passes only the INPUTS the rule
        # needs: the pre-retirement grown incomes, the resolved retirement
        # status, the base year-0 incomes (for the NET replacement target),
        # the resolved year-brackets, and the tax indexation rate.
        primary_income_pre=primary_income_pre,
        spouse_income_pre=spouse_income_pre,
        primary_retired=p_retired,
        spouse_retired=s_retired,
        base_primary_income=ctx.primary_income,
        base_spouse_income=ctx.spouse_income,
        year_brackets=year_brackets,
        tax_indexation_rate=ctx.tax_provider.indexation_rate,
        # Issue #761: compresses the discretionary portion of living_costs
        # under a shock when a split is declared (see apply_solvency).
        income_shock_active=income_shock_active,
        # Issue #679: cfg.living_costs is None when household_budget was
        # never supplied (DP#32 explicit-absence test, not `or`) -- 0.0 is
        # the solvency rule's own "not engaged" sentinel (DP#16).
        living_costs=cfg.living_costs if cfg.living_costs is not None else 0.0,
        after_tax_income=after_tax_income,
        # epic #795 bite 3: inputs for the registered tuition_credit rule --
        # the prologue passes the PRE-credit tax_before (above) + the tax
        # provider; the rule applies the credit inside simulate_year_pure and
        # apply_solvency adds the reduction to `available` and to
        # YearResult.after_tax_income (POST-credit, byte-identical to the
        # pre-refactor prologue output).
        tax_provider=ctx.tax_provider,
        primary_tax_before=primary_tax_before,
        spouse_tax_before=spouse_tax_before,
        # Issue #956 bite B (sale-core): the property_disposition rule bands a
        # sold property's gain against the owner's taxable income.
        primary_taxable_income=primary_taxable_income,
        spouse_taxable_income=spouse_taxable_income,
        borrowed_investment=borrowed_investment,
        # Issue #914: the non-borrowed year-0 free cash (RESP proceeds) invested
        # this year -- an inflow the solvency identity counts so the year-0
        # contribution it funds is not misread as a shortfall. 0.0 after year 0.
        free_cash_invested=free_cash_invested,
        # Epic #841 bite 2 / issue #812: the strategy's child-allocation targets
        # drive where each child's OWN savings land (the contract's opinion,
        # DP#13). Passing them (vs None) is what makes the fold MODEL the child
        # accounts this year -- grown once, by this real per-year step.
        child_allocation_pcts={
            'tfsa': ctx.strategy.child_tfsa_pct,
            'fhsa': ctx.strategy.child_fhsa_pct,
            'rrsp': ctx.strategy.child_rrsp_pct,
            'non_reg': ctx.strategy.child_non_reg_pct,
        },
        # Epic #841 bite 3: the per-child gift funding computed above, added to
        # each child's own savings inside the fold so it fills the child's
        # registered room. None-equivalent (all zeros) for the golden household.
        child_gift_amounts=child_gifts,
        # Issue #859 (Part A): the loan-kind subset, accumulated onto the child's
        # loan_funded_principal for the family balance sheet (all zeros golden).
        child_loan_amounts=child_loans,
        # Issue #899 (part a): each additional accumulating adult's OWN
        # end-of-year RRSP/TFSA (empty for a two-adult household).
        extra_adult_accounts=extra_adult_accounts,
        # Issue #1020 (S04 Step 1): prior-year GIS-countable income for the
        # retirement_income rule's gis_benefit call (CRA prior-year test).
        prior_gis_countable_income=prior_gis_countable_income,
    )
    # Issue #693 (epic #690 bite 2): surface this year's rental income on the
    # result. `net_rental_income` is the household total of each owner's net
    # rental income (operating income - deductible interest); the interest
    # deduction is surfaced separately so the s.20(1)(c) effect is observable.
    # Both 0.0 for a household with no couple-owned rental (the golden path).
    # Issue #694 (bite 3): the surfaced net rental income is the T776 figure
    # AFTER CCA (CCA is a T776 deduction line), so a declared CCA claim lowers
    # it -- even though the CASH the household keeps (in after_tax_income above)
    # is unchanged, since CCA is non-cash. `cca_claimed` surfaces the year's
    # depreciation and `rental_ucc` the running per-property UCC threaded to next
    # year for the estate recapture. All inert for a household with no CCA
    # election (the golden path -- net_rental_income unchanged from #693, DP#32).
    _total_cca = _p_rent_cca + _s_rent_cca
    result.net_rental_income = (
        (_p_rent_op - _p_rent_ded) + (_s_rent_op - _s_rent_ded) - _total_cca)
    result.rental_interest_deductible = _p_rent_ded + _s_rent_ded
    result.cca_claimed = _total_cca
    result.rental_ucc = _rental_ucc
    next_state.jurisdiction_state.setdefault('canada', {})['rental_ucc'] = _rental_ucc
    # Issue #697 (epic #690 bite 6): surface the SHORT-TERM-rental (Airbnb)
    # facts. `str_business_income` is the STR subset of `net_rental_income`
    # (business income, ITA s.9); `gst_hst_registration_required` is True when
    # any STR's gross rent exceeds the $30k small-supplier threshold. Both inert
    # (0.0 / False) for a household with no STR (the golden path, DP#32).
    result.str_business_income, result.gst_hst_registration_required = (
        _short_term_rental_facts(cfg))
    return result, next_state


# =============================================================================
# Simulation Engine
# =============================================================================

class FamilySimulation:
    """Year-by-year family financial simulation.
    
    Composable: accepts different strategies, rate paths, and configurations.
    Pure: produces results without modifying inputs.
    
    DP#8: Receives a JurisdictionAdapter for all jurisdiction-specific objects.
    DP#25: No direct imports from countries.canada in this module.
    
    Issue #21: __init__ is side-effect-free. All mutable state lives in
    SimState (self._state). No backward-compat properties or mutable account
    objects are stored on the instance.
    
    Args:
        config: Simulation parameters
        adapter: JurisdictionAdapter providing jurisdiction-specific factories
            (accounts, RESP, HELOC tracing, QC deduction, rate paths, strategies)
        strategy: How to allocate savings (overrides adapter default)
        rate_path: Mortgage rate path (overrides adapter default)
        use_readvanceable: Whether to use Readvanceable mortgage.
            None (default) = auto-detect from config.is_readvanceable (DP#16).
            True/False = explicit override (for optimizer comparisons).
        deduct_later: Whether to spread RRSP deductions over time.
            None (default) = auto-detect from config.should_deduct_later (DP#16).
            True/False = explicit override (for optimizer comparisons).
    """
    
    def __init__(self, config: SimulationConfig,
                 strategy: AllocationStrategy = None,
                 rate_path=None,
                 *,
                 adapter: JurisdictionAdapter = None,
                 use_readvanceable: bool = None,
                 deduct_later: bool = None,
                 lump_sum: float = 0.0,
                 free_cash: float = 0.0,
                 return_model=None):
        
        self.config = config
        
        # DP#8: Use jurisdiction adapter for all jurisdiction-specific objects.
        # DP#25: Prefer explicit adapter injection. If no adapter is provided,
        # default to CanadaAdapter.
        # deliberate act — core should not require it.
        if adapter is None:
            try:
                from countries.canada.adapter import CanadaAdapter
                adapter = CanadaAdapter(config)
            except ImportError as e:
                raise TypeError(
                    "FamilySimulation requires a JurisdictionAdapter. "
                    "Pass one explicitly, or install the Canada package:\n"
                    "  from countries.canada.adapter import CanadaAdapter\n"
                    "  sim = FamilySimulation(config, adapter=CanadaAdapter(config))"
                ) from e
        self.adapter = adapter
        
        self.strategy = strategy or adapter.get_default_strategy()
        self.rate_path = rate_path or adapter.build_rate_path(
            "Default", config.mortgage_rate, 10, 'variable', [config.mortgage_rate]
        )
        # Issue #654: the household's OWN declared HELOC rate
        # (SimulationConfig.heloc_rate, mapped from
        # liabilities[kind=heloc].rate) wins outright -- it is NEVER
        # derived from the mortgage's rate path. A cheap legacy fixed
        # mortgage alongside a prime-linked revolving HELOC is the
        # ordinary shape this tool exists for, not an edge case; charging
        # Smith-Manoeuvre borrowing at the mortgage rate silently flatters
        # leverage by understating its true after-tax cost.
        self.heloc_path = adapter.build_heloc_path(self.rate_path, heloc_rate=config.heloc_rate)
        # DP#16: Auto-detect readvanceable from config when not explicitly set.
        # When heloc_readvance=True AND margin_available > 0, the readvanceable
        # mortgage strategy is automatically applicable. The optimizer overrides
        # this by passing
        # use_readvanceable=True/False explicitly for comparison scenarios.
        if use_readvanceable is None:
            self.use_readvanceable = config.is_readvanceable
        else:
            self.use_readvanceable = use_readvanceable

        # Issue #654/DP#32: a HELOC that will actually carry a balance --
        # via the SM readvance strategy, or an undrawn-margin year-0 draw
        # (issue #577's margin_draw_for_lump_sum) -- must not silently
        # price that balance off an undeclared rate approximated from the
        # mortgage. The contract-driven path (input_contract.py) always
        # supplies heloc_rate when a HELOC liability exists (the schema
        # requires it), so this can only fire for a hand-built
        # SimulationConfig/internal-shape config that declares a
        # HELOC-drawing scenario without ever stating its rate -- surface
        # it loudly rather than let a plausible-looking number hide it.
        from simulation_state import margin_draw_for_lump_sum
        _will_draw_heloc = (
            self.use_readvanceable
            or margin_draw_for_lump_sum(lump_sum, config.margin_available) > 0
        )
        if config.heloc_rate is None and _will_draw_heloc:
            # DP#32: "the run must say so" -- logged, not raised. A hard
            # exception here would also fail every pre-existing hand-built
            # SimulationConfig in this test suite that exercises SM/margin-
            # draw scenarios (none of them could have set heloc_rate before
            # this field existed); a real contract can never reach this
            # branch at all (input_contract.py always supplies heloc_rate
            # when a HELOC liability exists -- the schema requires it, and
            # the mapping uses direct indexing that raises KeyError if that
            # guarantee is ever bypassed). logger.warning (not warnings.warn)
            # deliberately: this repo's pytest config treats warnings.warn
            # as a hard test failure (`filterwarnings = ["error"]`), which
            # would make this identical to a raise for every caller, real
            # contract or not.
            logger.warning(
                "This simulation will draw on a HELOC (readvanceable "
                "mortgage strategy and/or a year-0 margin draw) but "
                "SimulationConfig.heloc_rate was never set -- charging "
                "that balance interest at a rate approximated from the "
                "MORTGAGE's rate path (issue #654). Set property.heloc_rate "
                "(liabilities[kind=heloc].rate in a real contract) to price "
                "this correctly; the HELOC and the mortgage are different "
                "credit products and are not expected to share a rate."
            )
        # DP#16: Auto-detect deduct_later from config when not explicitly set.
        # When deduct_later_bracket_target > 0, the user has configured a bracket
        # target for RRSP deduction timing, which means the deduction-later
        # module should auto-activate. The optimizer overrides this by passing
        # deduct_later=True/False explicitly for comparison scenarios.
        if deduct_later is None:
            self.deduct_later = config.should_deduct_later
        else:
            self.deduct_later = deduct_later
        self.lump_sum = lump_sum  # Refinance cash-out for year-0 allocation
        self.free_cash = free_cash  # Free cash (e.g., RESP proceeds) — invested without adding to HELOC debt
        
        # DP#21/#260: Pluggable return model — compose through data, not hardcoded
        # float. return_model_data is the single source of truth: SimulationConfig
        # .from_dict materializes it from the deprecated investment_return scalar when
        # no return_model block is present. The investment_return fallback below only
        # serves SimulationConfig objects constructed directly (not via from_dict)
        # that set just the scalar.
        if return_model is None:
            from return_model import build_return_model_from_config, FixedReturn
            if config.return_model_data:
                self.return_model = build_return_model_from_config(config.return_model_data)
            else:
                self.return_model = FixedReturn(rate=config.investment_return)
        else:
            self.return_model = return_model
        
        # DP#27: Portfolio composition for income-type-specific after-tax returns.
        # When portfolio_data is present in config, non-reg investments grow
        # at income-type-specific after-tax rates (not a flat rate).
        # DP#16: Auto-include when portfolio data is present.
        self._portfolio = None
        if config.portfolio_data:
            try:
                from countries.canada.portfolio import PortfolioConfig
                self._portfolio = PortfolioConfig.from_dict(config.portfolio_data)
            except ImportError:
                pass  # Portfolio module not available — fall back to flat rate
        
        # Issue #21: All mutable state lives in SimState.
        # __init__ is side-effect-free: no mutable account objects, no I/O.
        # Lazy properties (tax_provider, amort, brackets, resp_calc, resp_children)
        # are computed from config/adapter on first access.
        #
        # Issue #583: the opening state (including any year-0 margin draw,
        # #577) is built by the single shared constructor
        # ``initial_state_for_run`` -- not by calling ``SimState.initial()``
        # and then re-deriving the draw locally. Every engine entry point
        # that starts a fold from scratch (this one, Optimizer._run_simulation,
        # DPOptimizer) calls the same constructor, so they cannot disagree
        # about what "the opening state" is.
        from simulation_state import initial_state_for_run
        self._state = initial_state_for_run(config, self.lump_sum)

        # Issue #1000 (DP#32): a declared fhsa_room_accumulated activates the
        # FHSA store (initial_state_for_run builds it from that key directly),
        # but StrategyEngine.allocate gates the FHSA sweep on
        # ``strategy.fhsa_pct > 0`` and every default/built-in strategy carries
        # ``fhsa_pct = 0.0`` -- so a household can declare room, watch the
        # engine build the store, and still route $0 to the FHSA for the whole
        # horizon: the parsed input never reaches the allocation decision.
        # Surface it. Logged, not raised (#654 precedent): an explicit
        # ``fhsa_pct = 0`` beside declared room is a legitimate "do not sweep
        # to the FHSA" instruction -- this warns that the room is inert, it
        # does not second-guess the zero -- and warnings.warn is a hard test
        # failure under this repo's filterwarnings=["error"] pytest config,
        # which would make it identical to a raise for every caller.
        from simulation_state import (
            adult_fhsa_total_room,
            adult_fhsa_total_lifetime_remaining,
        )
        _canada = self._state.jurisdiction_state.get('canada', {})
        if (adult_fhsa_total_room(_canada) > 0
                and adult_fhsa_total_lifetime_remaining(_canada) > 0
                and self.strategy.fhsa_pct <= 0):
            logger.warning(
                "This household declares FHSA contribution room "
                "(fhsa_room_accumulated), but the active strategy %r has "
                "fhsa_pct unset/0 -- no savings will be routed to the FHSA "
                "and the declared room moves nothing for the whole horizon "
                "(issue #1000). Set strategy.fhsa_pct > 0 to sweep savings "
                "into the FHSA.",
                self.strategy.name,
            )

    # ── Lazy properties (computed from config/adapter, not stored in __init__) ─
    
    @property
    def start_year(self):
        return self.config.start_year
    
    @property
    def frozen_brackets(self):
        return self.config.frozen_brackets
    
    @property
    def tax_provider(self):
        """DP#20: year-versioned tax data — lazily created from adapter."""
        if not hasattr(self, '_lazy_tax_provider'):
            from tax_data import TaxDataProvider
            provider = self.adapter.get_tax_provider()
            # Issue #295: index future-year bracket thresholds/limits by the
            # configured inflation assumption instead of freezing at base year.
            if self.config.inflation is not None:
                provider.indexation_rate = self.config.inflation
            self._lazy_tax_provider = provider
        return self._lazy_tax_provider
    
    @property
    def brackets(self):
        """Tax brackets — lazily computed from tax_provider."""
        if not hasattr(self, '_lazy_brackets'):
            self._lazy_brackets = self.tax_provider.get_combined_brackets()
        return self._lazy_brackets
    
    @property
    def amort(self):
        """Amortization schedule — lazily computed from adapter."""
        if not hasattr(self, '_lazy_amort'):
            self._lazy_amort = self.adapter.amortization_schedule(
                principal=self.config.mortgage_balance,
                rate_path=self.rate_path,
                amortization_years=self.config.amortization_years,
                projection_months=self.config.projection_years * 12,
                readvance_smith=self.use_readvanceable,
            )
        return self._lazy_amort
    
    @property
    def amort_annual(self):
        """Annual amortization summary — lazily computed from amort."""
        if not hasattr(self, '_lazy_amort_annual'):
            self._lazy_amort_annual = self.adapter.annual_summary(self.amort)
        return self._lazy_amort_annual
    
    @property
    def resp_calc(self):
        """RESP calculator — lazily created from adapter."""
        if not hasattr(self, '_lazy_resp_calc'):
            self._lazy_resp_calc = self.adapter.create_resp_calculator()
        return self._lazy_resp_calc
    
    @property
    def resp_children(self):
        """RESP child models — lazily created from adapter."""
        if not hasattr(self, '_lazy_resp_children'):
            children = []
            for ch in self.config.children:
                child_birth_year = ch.get('birth_year', self.config.start_year - ch.get('age', 0) if ch.get('age', 0) > 0 else 0)
                child = self.adapter.create_resp_child(
                    name=ch.get('name', 'Child'),
                    birth_year=child_birth_year,
                    province=ch.get('province', self.config.province),  # DP#8/#16: from config, not hardcoded
                    resp_balance=self.config.resp_current_balance / len(self.config.children) if self.config.children else 0,
                )
                children.append(child)
            self._lazy_resp_children = children
        return self._lazy_resp_children
    
    # ── Derived from config (no mutable state) ──
    
    @property
    def _primary_income(self):
        primary = next((m for m in self.config.family_members if m['role'] == 'primary'), {})
        return primary.get('gross_income', 0)
    
    @property
    def _spouse_income(self):
        spouse = next((m for m in self.config.family_members if m['role'] == 'spouse'), {})
        return spouse.get('gross_income', 0)

    @property
    def has_fhsa(self):
        """Whether FHSA is active — derived from jurisdiction_state, not a mutable object."""
        from simulation_state import adult_fhsa_active  # #700/#643: per-adult FHSA store
        canada = self._state.jurisdiction_state.get('canada', {})
        return adult_fhsa_active(canada)

    def _get_year_brackets(self, sim_year: int):
        """Get tax brackets for a simulation year, with warning on fallback.

        Thin delegator (DP#26/#583) over the pure ``_year_brackets_for``.
        """
        return _year_brackets_for(
            sim_year, frozen_brackets=self.frozen_brackets, brackets=self.brackets,
            tax_provider=self.tax_provider, province=self.config.province,
            start_year=self.start_year,
        )

    def _get_mortgage_data(self, year: int) -> Dict:
        """Extract mortgage data from amortization for a given year.

        Thin delegator (DP#26/#583) over the pure ``_mortgage_data_for``.
        """
        return _mortgage_data_for(year, amort_annual=self.amort_annual, amort=self.amort)

    def _get_non_reg_after_tax_return(self, year: int, primary_marginal_rate: float,
                                          gross_return: float) -> float:
        """DP#27 after-tax return for non-reg / SM investments (#575/#576).

        Thin delegator (DP#26/#583) over the pure
        ``_non_reg_after_tax_return_for`` -- see that function's docstring
        for the full rationale.
        """
        return _non_reg_after_tax_return_for(
            year, primary_marginal_rate, gross_return,
            portfolio=self._portfolio, non_reg_yield_rate=self.config.non_reg_yield_rate,
            province=self.config.province,
        )

    def _build_context(self) -> SimulationContext:
        """Gather everything the pure annual step needs from ``self``, once per call.

        Issue #583/DP#26: this is the ONLY place in ``FamilySimulation``
        that reads these attributes off ``self`` for the purpose of
        stepping a year -- it is the seam between the object and the pure
        fold, not the fold itself. Every field is either per-run static
        config/composition data (Barley's #575 handover: ``self.config``,
        ``self.strategy``, ``self.rate_path`` and friends "don't reproduce
        this failure mode") or a value read fresh from the household's
        CURRENT state at the moment this is called (``has_fhsa`` reads
        ``self._state``) -- never a growth balance frozen at ``__init__``
        time and consulted later as if current (that was the actual #575
        bug). Called fresh on every ``_simulate_year_step``/``run()`` call,
        so it cannot go stale across multiple ``run()`` invocations either.
        """
        return SimulationContext(
            config=self.config,
            strategy=self.strategy,
            rate_path=self.rate_path,
            heloc_path=self.heloc_path,
            use_readvanceable=self.use_readvanceable,
            deduct_later=self.deduct_later,
            lump_sum=self.lump_sum,
            free_cash=self.free_cash,
            return_model=self.return_model,
            tax_provider=self.tax_provider,
            amort_annual=self.amort_annual,
            amort=self.amort,
            resp_calc=self.resp_calc,
            resp_children=self.resp_children,
            has_fhsa=self.has_fhsa,
            frozen_brackets=self.frozen_brackets,
            brackets=self.brackets,
            start_year=self.start_year,
            primary_income=self._primary_income,
            spouse_income=self._spouse_income,
            portfolio=self._portfolio,
        )

    def _simulate_year_step(self, state, year: int,
                           prior_gis_countable_income=None) -> Tuple[YearResult, 'object']:
        """Pure annual step (DP#26/#583): (state, year) → (YearResult, next_state).

        Delegates to the module-level ``simulate_year(state, year, ctx)``,
        which reads nothing off ``self`` -- it isn't even a method, so
        there is no ``self`` in its scope to read. This method's only job
        is the one-line seam: gather ``self`` into a ``SimulationContext``
        (``_build_context()``) and hand it, plus the incoming ``state`` and
        ``year``, to the pure function. Folding this over
        ``range(projection_years)`` reproduces ``run`` exactly; neither this
        method nor ``simulate_year`` mutates ``self`` or the incoming
        ``state``.
        """
        return simulate_year(state, year, self._build_context(),
                              prior_gis_countable_income=prior_gis_countable_income)

    def run(self) -> List[YearResult]:
        """Run the full simulation as a fold over annual steps (DP#26).

        ``run`` is a thin fold: starting from ``self._state`` it reduces
        ``_simulate_year_step`` over every projection year, threading the
        immutable ``SimState`` from one year into the next and collecting one
        ``YearResult`` per year. There is no imperative accumulation in the loop
        body itself — each year's outputs are a pure function of the prior
        state. The final folded state is published to ``self._state`` once,
        after the fold completes (the only mutation), preserving the previous
        contract exactly.
        """
        if self.config.time_step == 'monthly':
            return self._run_monthly()

        from functools import reduce

        def step(acc, year):
            results, state = acc
            # Issue #1020 (S04 Step 1): GIS uses the PRIOR year's income test
            # (CRA). Thread the prior year's GIS-countable income (retirement
            # income excluding OAS) from the prior YearResult. None for year 0
            # (no prior year) -- the retirement_income rule leaves GIS at 0.
            prior_gis = _prior_gis_countable(results)
            result, next_state = self._simulate_year_step(state, year, prior_gis)
            results.append(result)
            return results, next_state

        results, final_state = reduce(
            step, range(self.config.projection_years), ([], self._state))

        self._state = final_state
        # Issue #681: the charge invariants run HERE, in the engine, on every
        # run -- not only in a fixture. #664 defined them correctly and left
        # them unwired, so the charge held at t=0 and was breached by t=1 and
        # nothing noticed. A trajectory that borrowed past the charge
        # registered against the property is not a pessimistic answer, it is
        # not an answer at all -- raise (DP#32), never rank it.
        assert_run_invariants(results, self.config)
        return results

    def _run_monthly(self) -> List[YearResult]:
        """Run simulation with monthly time steps using SimState pure-function fold.
        
        DP#26: The monthly path now uses simulate_year_pure as the core engine,
        threading SimState through each year. Monthly allocation decisions are made
        12 times per year (strategy engine with monthly_savings = annual_savings / 12),
        accumulated into annual totals, then applied via a single simulate_year_pure
        call per year. Monthly compounding effect is approximated using effective
        annual return: (1 + r/12)^12 - 1.
        
        This ensures ACB tracking, HELOC tracing, Quebec deduction carry-forward,
        and RRSP per-contribution deduction tracking all work correctly in monthly mode.
        """
        from simulation_state import (
            simulate_year_pure, child_savings_for_year, child_gift_funding_for_year,
            child_loan_funded_for_year,
            child_after_tax_savings_for_year,
            adult_rrsp_slot, adult_tfsa_slot,
            adult_fhsa_total_room, adult_fhsa_total_lifetime_remaining)
        from strategy import StrategyEngine
        
        cfg = self.config
        results = []
        state = self._state
        salary_growth = cfg.salary_growth
        # Issue #674: looked up once, reused by both the year-0 lump-sum
        # block and the per-year loop below.
        primary_member = next((m for m in cfg.family_members if m.get('role') == 'primary'), {})
        spouse_member = next((m for m in cfg.family_members if m.get('role') == 'spouse'), {})

        # Process lump-sum at year 0 start (same as yearly path)
        # Issue #914: the year-0 allocation fires for a borrowed lump sum OR
        # for non-borrowed free cash (RESP-collapse/EAP proceeds) -- the monthly
        # path must consume free_cash the same way the yearly fold does, or the
        # two time-steps diverge (DP#9). free_cash rides in as an extra non-reg
        # contribution with no debt and no s.20(1)(c) tracing; when only free
        # cash is present, fill_room(0) returns all zeros so borrowed_investment
        # stays 0.
        if (self.lump_sum > 0 or self.free_cash > 0) and state is not None:
            # Issue #674: honour a dated income_segment covering the
            # lump-sum's own calendar year (start_year) the same way the
            # per-year loop below does -- a job-loss scenario whose window
            # opens at year 0 must apply to the year-0 allocation too.
            primary_income, primary_earned_income = _income_components_for_year(
                self._primary_income, primary_member.get('income_segments'),
                self.start_year, salary_growth, 0)
            spouse_income, spouse_earned_income = _income_components_for_year(
                self._spouse_income, spouse_member.get('income_segments'),
                self.start_year, salary_growth, 0)
            primary_rate = marginal_rate(primary_income, self.brackets)
            spouse_rate = marginal_rate(spouse_income, self.brackets) if spouse_income > 0 else 0
            canada = state.jurisdiction_state.get('canada', {})
            lump_state = FamilyState(
                primary_income=primary_income,
                spouse_income=spouse_income,
                primary_marginal_rate=primary_rate,
                spouse_marginal_rate=spouse_rate,
                primary_rrsp_room=adult_rrsp_slot(canada, 0)[1],  # #700: per-adult own_room
                spouse_rrsp_room=adult_rrsp_slot(canada, 1)[1],
                primary_tfsa_room=adult_tfsa_slot(canada, 0)[1],  # #700: per-adult room
                spouse_tfsa_room=adult_tfsa_slot(canada, 1)[1],
                fhsa_room=adult_fhsa_total_room(canada) if self.has_fhsa else 0,  # #893: total household room
                fhsa_lifetime_remaining=adult_fhsa_total_lifetime_remaining(canada) if self.has_fhsa else 0,
                resp_eligible_children=0,
                annual_savings=0,
                bracket_gap=primary_rate - spouse_rate,
            )
            engine = StrategyEngine(self.strategy)
            # Issue #792: honour a declared deductible-vs-registered advance
            # split (None = today's registered-first internal optimization).
            lump_alloc = engine.fill_room(
                self.lump_sum, lump_state,
                deductible_non_reg_first=self.config.refinance_advance_deductible_non_reg,
            )
            
            # Apply lump sum as year-0 allocation via simulate_year_pure
            lump_allocations = {
                'primary_rrsp': lump_alloc.primary_rrsp,
                'spousal_rrsp': lump_alloc.spousal_rrsp,
                'spouse_rrsp': lump_alloc.spouse_rrsp,
                'primary_tfsa': lump_alloc.primary_tfsa,
                'spouse_tfsa': lump_alloc.spouse_tfsa,
                'fhsa': lump_alloc.fhsa,
                'resp': 0,
                # Issue #914: borrowed non-reg + non-borrowed free cash. Only
                # the borrowed portion feeds _lump_sum/_lump_non_reg below.
                'non_reg': lump_alloc.non_reg + self.free_cash,
                '_primary_income': primary_income,
                '_spouse_income': spouse_income,
                '_primary_earned_income': primary_earned_income,
                '_spouse_earned_income': spouse_earned_income,
                '_annual_savings': 0,
                # Issue #850: this path's spelling of the same year-0 fact the
                # yearly fold passes -- the borrowed lump's non-registered
                # (income-producing, hence s.20(1)(c)-qualifying) portion, read
                # off the SAME `fill_room` allocation. Here it is unambiguously
                # the whole lump's non-reg share: this dict carries ONLY the
                # lump-sum allocation, with no salary-funded savings mixed in.
                '_lump_sum': self.lump_sum,
                '_lump_non_reg': lump_alloc.non_reg,
            }
            lump_return = self.return_model.return_for_year(0)
            result0, state = simulate_year_pure(
                state=state,
                year=0,
                calendar_year=self.start_year,  # issue #343: calendar year for date-computed gates
                allocations=lump_allocations,
                config=cfg,
                investment_return=lump_return,
                mortgage_rate=self.rate_path.get_rate(0),
                heloc_rate=self.heloc_path.get_heloc_rate(0, self.rate_path.rate_type),
                mortgage_data=self._get_mortgage_data(0),
                use_readvanceable=self.use_readvanceable,
                deduct_later=self.deduct_later,
                primary_marginal_rate=primary_rate,
                spouse_marginal_rate=spouse_rate,
                resp_data=None,
                fhsa_contribution=lump_alloc.fhsa if self.has_fhsa else 0.0,
                fhsa_annual_limit=self.tax_provider.get_fhsa_limit(self.start_year) if self.has_fhsa else None,
                rrsp_annual_limit=self.tax_provider.get_rrsp_limit(self.start_year),
                tfsa_annual_limit=self.tax_provider.get_tfsa_limit(self.start_year),
                non_reg_after_tax_return=self._get_non_reg_after_tax_return(
                    0, primary_rate, lump_return),
                registered_wht_drag=_registered_wht_drag_for(self._portfolio),
                # Issue #679: every dollar of this lump is BORROWED (margin draw
                # + mortgage cash-out), so it is an inflow as well as an outflow.
                # The solvency rule is inert on this monthly path today (it never
                # receives living_costs, so it no-ops), but threading this now
                # means whoever wires the budget through here does not silently
                # reintroduce the false-ruin-on-leverage bug.
                borrowed_investment=(
                    lump_alloc.primary_rrsp + lump_alloc.spousal_rrsp
                    + lump_alloc.spouse_rrsp + lump_alloc.primary_tfsa
                    + lump_alloc.spouse_tfsa + lump_alloc.fhsa + lump_alloc.non_reg
                ),
                # Issue #914: the non-borrowed free cash invested this year --
                # the inflow that funds its own non-reg contribution (no debt).
                free_cash_invested=self.free_cash,
            )
            # Lump sum uses year 0 result; don't append to results (it's pre-projection)

        for year in range(cfg.projection_years):
            sim_year = self.start_year + year

            # ── Compute incomes for this year (issue #674: dated
            # income_segments, not just the base salary-grown flat amount) ──
            primary_income, primary_earned_income = _income_components_for_year(
                self._primary_income, primary_member.get('income_segments'),
                sim_year, salary_growth, year)
            spouse_income, spouse_earned_income = _income_components_for_year(
                self._spouse_income, spouse_member.get('income_segments'),
                sim_year, salary_growth, year)
            # Issue #978: each member's SELF-EMPLOYMENT income slice for the
            # year -- the base for the mandatory Quebec self-employed
            # contribution stack the working-phase cash flow charges below.
            # $0 for a member with no `kind == 'self_employment'` segment
            # (DP#32 absence-safe), so the golden household is a no-op.
            primary_self_emp = _self_employment_income_for_year(
                self._primary_income, primary_member.get('income_segments'),
                sim_year, salary_growth, year)
            spouse_self_emp = _self_employment_income_for_year(
                self._spouse_income, spouse_member.get('income_segments'),
                sim_year, salary_growth, year)
            # Issue #761: a working-life year whose income is reduced below
            # baseline by a dated decisions.income[] shock -- compresses the
            # discretionary portion of living_costs in apply_solvency when a
            # split is declared.
            income_shock_active = (
                _income_shock_active_for_year(
                    self._primary_income, primary_member.get('income_segments'),
                    sim_year, salary_growth, year)
                or _income_shock_active_for_year(
                    self._spouse_income, spouse_member.get('income_segments'),
                    sim_year, salary_growth, year))

            # ── Issue #294 / epic #795 bite 1: retirement transition ──
            # Once a member reaches retirement_age, stop their employment
            # income (no salary, no further salary growth) so the monthly
            # allocation strategy and the marginal-rate / after-tax / savings
            # math below see the RETIRED income figure. The CPP/OAS/pension +
            # drawdown-sizing half of the transition is now a REGISTERED RULE
            # (``retirement_income`` in simulation_rules.py) that runs inside
            # simulate_year_pure and writes its outputs to YearWorkingState;
            # the prologue passes only the retirement STATUS (a date-computed
            # eligibility gate, DP#1/#28) and the pre-retirement grown incomes
            # + brackets + indexation rate the rule needs. DP#25: lazy import.
            from countries.canada.retirement_transition import (
                is_retired, DEFAULT_RETIREMENT_AGE,
            )
            p_retired = is_retired(
                primary_member.get('birth_year', 0),
                primary_member.get('retirement_age', DEFAULT_RETIREMENT_AGE),
                sim_year) if primary_member else False
            s_retired = is_retired(
                spouse_member.get('birth_year', 0),
                spouse_member.get('retirement_age', DEFAULT_RETIREMENT_AGE),
                sim_year) if spouse_member else False
            primary_income_pre, spouse_income_pre = primary_income, spouse_income
            if p_retired:
                primary_income = 0.0
                primary_earned_income = 0.0
                # Issue #978: the stack is on WORKING self-employment income --
                # a retired member earns no self-employment salary.
                primary_self_emp = 0.0
            if s_retired:
                spouse_income = 0.0
                spouse_earned_income = 0.0
                spouse_self_emp = 0.0

            total_income = primary_income + spouse_income
            for ch in cfg.children:
                if ch.get('gross_income', 0) > 0:
                    total_income += ch['gross_income'] * (1 + salary_growth) ** year

            annual_savings = total_income * cfg.savings_rate
            
            # DP#18: apply CashFlow events
            for cf in cfg.cash_flows:
                if cf.get('year', 0) == sim_year:
                    cf_amount = cf.get('amount', 0)
                    cf_tax = cf.get('tax_treatment', 'post-tax')
                    if cf_tax in ('pre-tax', 'non-taxable'):
                        annual_savings += cf_amount
                    else:
                        annual_savings += cf_amount
            
            # ── Marginal rates (year-specific brackets per DP#20) ──
            year_brackets = self._get_year_brackets(sim_year)

            # Issue #679: after-tax employment income for the cash-flow
            # solvency identity (see simulate_year's identical computation
            # for the full rationale). Issue #764: subtract each taxed
            # member's OWN federal (+ QC provincial) tuition credit from their
            # tax. Issue #784: the credit is NON-REFUNDABLE -- unused portions
            # CARRY FORWARD to reduce a future year's tax (per-member state on
            # SimState, threaded by the fold).
            # Issue #813: private loan interest -- lender accrues taxable
            # interest income (their bracket); borrower's interest deductible
            # under s.20(1)(c) only when use=investment. Applied to taxable
            # income before tax_on_income (reuses the income-reduction trace,
            # not a new deduction mechanism). All zero when no private loans
            # are declared (the golden household); minor-lender attribution
            # (s.74.2) is handled inside the helper.
            _p_loan_inc, _s_loan_inc, _p_loan_ded, _s_loan_ded = _private_loan_interest_for(
                cfg, sim_year, primary_member, spouse_member)
            # Issue #693 (epic #690 bite 2): a declared rental's net rental income
            # + s.20(1)(c) interest deduction (see simulate_year's identical
            # block). Zero when no couple-owned rental is declared.
            # Issue #694 (bite 3): a declared CCA election also depreciates the
            # building (non-cash deduction) and tracks a declining UCC threaded
            # through jurisdiction_state['canada']['rental_ucc'] -- read from the
            # opening state here, exactly as the yearly path does, so monthly and
            # yearly agree (the parity tests). Inert (0.0 / {}) with no CCA.
            (_p_rent_op, _s_rent_op, _p_rent_ded, _s_rent_ded,
             _p_rent_cca, _s_rent_cca, _rental_ucc) = _rental_income_for(
                cfg, sim_year, primary_member, spouse_member,
                state.jurisdiction_state.get('canada', {}).get('rental_ucc', {}))
            # Issue #701 (Step 5 of #643): tax each adult individually via a
            # loop over config.adults() (see simulate_year's identical block).
            # Isomorphic to the former hardcoded primary/spouse pair for the
            # two-adult household; the two-slot signature is kept this step.
            # Issue #899 (part a): additional accumulating adults taxed
            # individually too (same as the annual path). Empty for a two-adult
            # household -> byte-identical.
            # Issue #693: rental operating income + interest deduction ride the
            # same per-role taxable-income adjustment as the loan (DP#9).
            # Issue #694: CCA is an additional NON-CASH deduction against rental
            # income, so it joins the interest deduction in the TAXABLE-income
            # adjustment but -- unlike the interest -- is NOT subtracted from
            # after-tax cash below (depreciation lowers tax without consuming cash).
            _extra_specs = _extra_adult_specs(cfg, sim_year, salary_growth, year, year_brackets)
            _income_by_role, _loan_by_role = _adult_income_maps(
                primary_income, spouse_income,
                (_p_loan_inc + _p_rent_op, _p_loan_ded + _p_rent_ded + _p_rent_cca),
                (_s_loan_inc + _s_rent_op, _s_loan_ded + _s_rent_ded + _s_rent_cca),
                _extra_specs)
            _adult_tax = _income_tax_by_adult(cfg, _income_by_role, _loan_by_role, year_brackets)
            primary_rate = _adult_tax['primary']['rate']
            spouse_rate = _adult_tax['spouse']['rate']
            primary_tax_before = _adult_tax['primary']['tax_before']
            spouse_tax_before = _adult_tax['spouse']['tax_before']
            # Issue #956 bite B (sale-core): the taxable income base the
            # property_disposition rule bands a sold property's gain against
            # (mirrors the annual path, DP#9 -- one spelling).
            primary_taxable_income = _adult_tax['primary']['taxable_income']
            spouse_taxable_income = _adult_tax['spouse']['taxable_income']
            # Epic #795 bite 3: the tuition credit is no longer applied here --
            # it is a REGISTERED RULE (``tuition_credit`` in simulation_rules.py)
            # that runs inside simulate_year_pure and writes the per-member tax
            # reduction to YearWorkingState; apply_solvency adds it to
            # `available`. The prologue passes the PRE-credit tax_before (above)
            # + the tax provider; after_tax_income below is PRE-credit, and the
            # rule + apply_solvency restore the POST-credit figure on
            # YearResult (byte-identical to the annual path).
            # Issue #813: the family-loan interest is an intra-household
            # transfer that nets to zero in household cash income; only its tax
            # effects remain (tax computed on interest-adjusted taxable income
            # above, after_tax_income stays on base income).
            # Issue #701 (Step 5): summed per adult over config.adults().
            # Issue #693: a rental's NET rental income is real household cash
            # (paid by an outside tenant), so it IS added to after-tax income
            # (see the annual path). Zero for a household with no rental (DP#32).
            # Issue #978: the mandatory Quebec self-employed contribution stack
            # (QPP both halves + QPIP self-employed + individual HSF) is a
            # pre-savings cash OUTFLOW the bracket-only tax path never charged.
            # $0 for a member with no self-employment income, so an employee /
            # the golden household is byte-for-byte unchanged (DP#32).
            primary_contrib_stack = _self_employed_contribution_stack(
                primary_self_emp, cfg.province, sim_year)
            spouse_contrib_stack = _self_employed_contribution_stack(
                spouse_self_emp, cfg.province, sim_year)
            _after_tax_by_role = {
                'primary': primary_income + (_p_rent_op - _p_rent_ded) - primary_tax_before
                - primary_contrib_stack,
                'spouse': spouse_income + (_s_rent_op - _s_rent_ded) - spouse_tax_before
                - spouse_contrib_stack,
            }
            # Issue #899 (part a): household solvency is the PRIMARY couple's
            # after-tax income only (see the annual path for the full rationale);
            # byte-identical for a two-adult household.
            after_tax_income = sum(
                _after_tax_by_role[adult['role']] for adult in cfg.adults()
                if adult['role'] in ('primary', 'spouse'))

            # ── Monthly allocation: compute 12 monthly allocations, accumulate into annual totals ──
            # Monthly compounding is approximated by using effective annual return:
            # (1 + r/12)^12 - 1. This preserves the pure-function fold (DP#26)
            # while capturing the compounding effect.
            ret_annual = self.return_model.return_for_year(year)
            ret_effective = (1 + ret_annual / 12) ** 12 - 1  # Effective annual with monthly compounding

            # Epic #841 bite 2 / issue #812: carve each child's OWN savings out
            # of the household allocation base (a child's OWN income funds the
            # child's OWN accounts, grown below by simulate_year_pure -- DP#18:
            # redirected, not created). sum() is 0 for a household with no child
            # income, so the ADULT allocation base is unchanged there. The full
            # annual_savings still flows to the solvency identity below
            # (allocations['_annual_savings']) -- only the ADULT contribution
            # base shrinks (DP#9: same carve the yearly path applies).
            child_savings = child_savings_for_year(
                cfg.children, cfg.savings_rate, salary_growth, year)
            # Issue #701 Step 6: the adult-base carve stays GROSS (fold + carve
            # are one artifact pair, netting to zero on the adult base); the
            # amount that LANDS in the child's accounts is the child's AFTER-TAX
            # income (taxed individually on the child's own return, own bracket),
            # so gift room-capping uses the after-tax figure. Both are 0 for a
            # zero-income child (the golden household). See simulate_year.
            child_savings_after_tax = child_after_tax_savings_for_year(
                cfg.children, cfg.savings_rate, salary_growth, year, year_brackets)
            # Epic #841 bite 3: parent->child gifts fund the child's registered
            # room beyond the child's own income, carved out of the ADULT base
            # too (DP#18: redirected, not created; capped to the child's
            # remaining registered room after their own after-tax savings). Zero
            # for the golden household.
            child_gifts = child_gift_funding_for_year(
                cfg.children, getattr(cfg, 'gifts', []), child_savings_after_tax,
                state.jurisdiction_state.get('canada', {}).get('child_accounts', []))
            adult_annual_savings = annual_savings - sum(child_savings) - sum(child_gifts)
            # Issue #859 (Part A): the loan-kind subset of that funding, threaded
            # onto loan_funded_principal for the family balance sheet (DP#18).
            child_loans = child_loan_funded_for_year(
                cfg.children, getattr(cfg, 'gifts', []), child_savings_after_tax,
                state.jurisdiction_state.get('canada', {}).get('child_accounts', []))

            # FHSA is an annual contribution — allocate it first from annual savings
            fhsa_contrib = 0.0
            if self.has_fhsa:
                canada = state.jurisdiction_state.get('canada', {})
                fhsa_state = FamilyState(
                    primary_income=primary_income,
                    spouse_income=spouse_income,
                    primary_marginal_rate=primary_rate,
                    spouse_marginal_rate=spouse_rate,
                    primary_rrsp_room=adult_rrsp_slot(canada, 0)[1],  # #700: per-adult own_room
                    spouse_rrsp_room=adult_rrsp_slot(canada, 1)[1],
                    primary_tfsa_room=adult_tfsa_slot(canada, 0)[1],  # #700: per-adult room
                    spouse_tfsa_room=adult_tfsa_slot(canada, 1)[1],
                    fhsa_room=adult_fhsa_total_room(canada),  # #893: total household room
                    fhsa_lifetime_remaining=adult_fhsa_total_lifetime_remaining(canada),
                    resp_eligible_children=sum(1 for c in self.resp_children if c.cesg_eligible(sim_year)),
                    resp_annual_match_cap=self.resp_calc.cesg_contribution_max(sim_year),  # #1046
                    annual_savings=adult_annual_savings,
                    bracket_gap=primary_rate - spouse_rate,
                )
                fhsa_engine = StrategyEngine(self.strategy)
                fhsa_alloc = fhsa_engine.allocate(fhsa_state)
                fhsa_contrib = fhsa_alloc.fhsa

            # Monthly savings after deducting annual FHSA contribution
            monthly_savings = (adult_annual_savings - fhsa_contrib) / 12
            
            # Accumulate 12 monthly allocations into annual totals
            # (Each month gets the same allocation; the strategy allocates
            # 1/12 of remaining savings after FHSA)
            accum = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                     'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                     'resp': 0, 'non_reg': 0}
            
            for month in range(12):
                _canada = state.jurisdiction_state.get('canada', {})
                month_state = FamilyState(
                    primary_income=primary_income,
                    spouse_income=spouse_income,
                    primary_marginal_rate=primary_rate,
                    spouse_marginal_rate=spouse_rate,
                    primary_rrsp_room=adult_rrsp_slot(_canada, 0)[1] - accum['primary_rrsp'] - accum['spousal_rrsp'],  # #700
                    spouse_rrsp_room=adult_rrsp_slot(_canada, 1)[1] - accum['spouse_rrsp'],
                    primary_tfsa_room=adult_tfsa_slot(_canada, 0)[1] - accum['primary_tfsa'],  # #700
                    spouse_tfsa_room=adult_tfsa_slot(_canada, 1)[1] - accum['spouse_tfsa'],
                    fhsa_room=0,  # FHSA already allocated annually
                    fhsa_lifetime_remaining=0,
                    resp_eligible_children=sum(1 for c in self.resp_children if c.cesg_eligible(sim_year)),
                    resp_annual_match_cap=self.resp_calc.cesg_contribution_max(sim_year),  # #1046
                    annual_savings=monthly_savings,
                    bracket_gap=primary_rate - spouse_rate,
                )
                month_engine = StrategyEngine(self.strategy)
                month_alloc = month_engine.allocate(month_state)
                accum['primary_rrsp'] += month_alloc.primary_rrsp
                accum['spousal_rrsp'] += month_alloc.spousal_rrsp
                accum['spouse_rrsp'] += month_alloc.spouse_rrsp
                accum['primary_tfsa'] += month_alloc.primary_tfsa
                accum['spouse_tfsa'] += month_alloc.spouse_tfsa
                accum['resp'] += month_alloc.resp
                accum['non_reg'] += month_alloc.non_reg
            accum['fhsa'] = fhsa_contrib
            
            # ── Build allocations dict for pure function ──
            allocations = {
                'primary_rrsp': accum['primary_rrsp'],
                'spousal_rrsp': accum['spousal_rrsp'],
                'spouse_rrsp': accum['spouse_rrsp'],
                'primary_tfsa': accum['primary_tfsa'],
                'spouse_tfsa': accum['spouse_tfsa'],
                'fhsa': accum['fhsa'],
                'resp': accum['resp'],
                'non_reg': accum['non_reg'],
                '_primary_income': primary_income,
                '_spouse_income': spouse_income,
                '_primary_earned_income': primary_earned_income,
                '_spouse_earned_income': spouse_earned_income,
                '_annual_savings': annual_savings,
            }
            
            # ── Compute RESP CESG/QESI grants ──
            resp_data = []
            if self.resp_children:
                for ch in self.resp_children:
                    ch_contrib = accum['resp'] / max(1, len(self.resp_children))
                    rd = {'contribution': ch_contrib, 'cesg': 0.0, 'qesi': 0.0}
                    if ch.cesg_eligible(sim_year):
                        cesg_result = self.resp_calc.calculate_cesg(
                            ch_contrib, ch, sim_year, total_income)
                        qesi_result = self.resp_calc.calculate_qesi(
                            ch_contrib, ch, sim_year, total_income)
                        rd['cesg'] = cesg_result['total_cesg']
                        rd['qesi'] = qesi_result['total_qesi']
                        # #1046: advance lifetime state so CESG/QESI caps bind
                        ch.total_cesg_received += cesg_result['total_cesg']
                        ch.total_qesi_received += qesi_result['total_qesi']
                        ch.total_contributions += ch_contrib
                        age = sim_year - ch.birth_year
                        if age <= 15:
                            ch.total_before_age_15 += ch_contrib
                        ch.contribution_years.append((sim_year, ch_contrib))
                    else:
                        ch.total_contributions += ch_contrib
                        age = sim_year - ch.birth_year
                        if age <= 15:
                            ch.total_before_age_15 += ch_contrib
                        ch.contribution_years.append((sim_year, ch_contrib))
                    resp_data.append(rd)
            
            # ── Rates and mortgage data ──
            mortgage_rate = self.rate_path.get_rate(year)
            heloc_rate = self.heloc_path.get_heloc_rate(year, self.rate_path.rate_type)
            mort = self._get_mortgage_data(year)
            
            # ── Pure simulation step (DP#26: same inputs -> same outputs) ──
            # DP#20: year-specific contribution limits
            rrsp_limit = self.tax_provider.get_rrsp_limit(sim_year)
            tfsa_limit = self.tax_provider.get_tfsa_limit(sim_year)
            
            # DP#27: Compute income-type-specific after-tax return for non-reg
            non_reg_atr = self._get_non_reg_after_tax_return(
                year, primary_rate, ret_effective)

            # Issue #899 (part a): grow each additional accumulating adult's OWN
            # RRSP/TFSA (empty for a two-adult household). Uses the monthly
            # effective annual return, the same rate this path grows every other
            # account by.
            from simulation_state import step_extra_adult_accounts as _step_extra_adults
            _opening_canada = state.jurisdiction_state.get('canada', {})
            extra_adult_accounts = _step_extra_adults(
                _opening_canada.get('adult_rrsp', {}), _opening_canada.get('adult_tfsa', {}),
                _extra_specs, ret_effective, rrsp_limit, tfsa_limit)

            result, state = simulate_year_pure(
                state=state,
                year=year,
                calendar_year=sim_year,  # issue #343: drives LIRA→LIF conversion at age 71
                allocations=allocations,
                config=cfg,
                investment_return=ret_effective,
                mortgage_rate=mortgage_rate,
                heloc_rate=heloc_rate,
                mortgage_data=mort,
                use_readvanceable=self.use_readvanceable,
                deduct_later=self.deduct_later,
                primary_marginal_rate=primary_rate,
                spouse_marginal_rate=spouse_rate,
                resp_data=resp_data if resp_data else None,
                fhsa_contribution=fhsa_contrib,
                fhsa_annual_limit=self.tax_provider.get_fhsa_limit(sim_year) if self.has_fhsa else None,
                rrsp_annual_limit=rrsp_limit,
                tfsa_annual_limit=tfsa_limit,
                non_reg_after_tax_return=non_reg_atr,
                registered_wht_drag=_registered_wht_drag_for(self._portfolio),
                # epic #795 bite 1: the retirement transition OUTPUTS are no
                # longer passed by the prologue -- the registered
                # `retirement_income` rule computes them inside
                # simulate_year_pure and writes them to YearWorkingState.
                # The prologue passes only the INPUTS the rule needs (see
                # simulate_year's identical block for the full rationale).
                primary_income_pre=primary_income_pre,
                spouse_income_pre=spouse_income_pre,
                primary_retired=p_retired,
                spouse_retired=s_retired,
                base_primary_income=self._primary_income,
                base_spouse_income=self._spouse_income,
                year_brackets=year_brackets,
                tax_indexation_rate=self.tax_provider.indexation_rate,
                # Issue #761: compresses the discretionary portion of
                # living_costs under a shock when a split is declared.
                income_shock_active=income_shock_active,
                living_costs=cfg.living_costs if cfg.living_costs is not None else 0.0,
                after_tax_income=after_tax_income,
                # epic #795 bite 3: inputs for the registered tuition_credit
                # rule (see the annual path's identical block). The prologue
                # passes the PRE-credit tax_before + the tax provider; the rule
                # applies the credit inside simulate_year_pure and
                # apply_solvency restores the POST-credit figure on
                # YearResult.after_tax_income.
                tax_provider=self.tax_provider,
                primary_tax_before=primary_tax_before,
                spouse_tax_before=spouse_tax_before,
                # Issue #956 bite B (sale-core): the taxable income base the
                # property_disposition rule bands a sold property's gain against
                # (mirrors the annual path, DP#9).
                primary_taxable_income=primary_taxable_income,
                spouse_taxable_income=spouse_taxable_income,
                # Epic #841 bite 2 / issue #812: model each child's OWN accounts
                # this year (DP#9: same targets, same fold step as the yearly
                # path). The year-0 lump-sum PRE-step above passes no such
                # targets, so a child's accounts grow exactly ONCE per year --
                # here, in this real per-year step.
                child_allocation_pcts={
                    'tfsa': self.strategy.child_tfsa_pct,
                    'fhsa': self.strategy.child_fhsa_pct,
                    'rrsp': self.strategy.child_rrsp_pct,
                    'non_reg': self.strategy.child_non_reg_pct,
                },
                # Epic #841 bite 3: the per-child gift funding computed above.
                child_gift_amounts=child_gifts,
                # Issue #859 (Part A): the loan-kind subset for the balance sheet.
                child_loan_amounts=child_loans,
                # Issue #899 (part a): each additional accumulating adult's OWN
                # end-of-year RRSP/TFSA (empty for a two-adult household).
                extra_adult_accounts=extra_adult_accounts,
                # Issue #1020 (S04 Step 1): prior-year GIS-countable income
                # for the retirement_income rule's gis_benefit call (CRA
                # prior-year test). None for year 0 (no prior year).
                prior_gis_countable_income=_prior_gis_countable(results),
            )
            # Issue #693 (epic #690 bite 2): surface this year's rental income
            # (see simulate_year's identical block). Issue #694 (bite 3): the
            # surfaced net rental income is the T776 figure AFTER CCA (non-cash,
            # so after_tax_income above is unchanged); cca_claimed and the running
            # per-property UCC are surfaced and threaded to next year so the
            # estate can recapture the CCA. All inert with no CCA election.
            _total_cca = _p_rent_cca + _s_rent_cca
            result.net_rental_income = (
                (_p_rent_op - _p_rent_ded) + (_s_rent_op - _s_rent_ded) - _total_cca)
            result.rental_interest_deductible = _p_rent_ded + _s_rent_ded
            result.cca_claimed = _total_cca
            result.rental_ucc = _rental_ucc
            state.jurisdiction_state.setdefault('canada', {})['rental_ucc'] = _rental_ucc
            # Issue #697 (bite 6): surface the STR (Airbnb) facts (see
            # simulate_year's identical block). Inert for a household with no STR.
            result.str_business_income, result.gst_hst_registration_required = (
                _short_term_rental_facts(cfg))
            results.append(result)

        # Update canonical state
        self._state = state

        # Issue #681: the monthly fold is a second traversal of the same year
        # step, so it gets the same charge guard -- an invariant wired into
        # only one of two run paths is exactly the half-enforcement #681 is
        # about.
        assert_run_invariants(results, self.config)

        return results
    

    def summary(self) -> Dict:
        """Return a summary of the current simulation state."""
        from simulation_state import adult_rrsp_total, adult_tfsa_total
        state = self._state
        canada = state.jurisdiction_state.get('canada', {})
        return {
            'total_assets': state.total_assets(),
            'total_debt': state.total_debt(),
            'net_assets': state.net_assets(),
            'rrsp_balance': adult_rrsp_total(canada),  # #700/#643: per-adult store
            'tfsa_balance': adult_tfsa_total(canada),  # #700/#643: per-adult store
            'non_reg_balance': state.non_reg_balance,
            'mortgage_balance': state.mortgage_balance,
            'heloc_balance': state.heloc_balance + canada.get('readvance_heloc_balance', 0),
        }