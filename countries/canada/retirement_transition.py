#!/usr/bin/env python3
"""
Retirement Transition — per-member income stop + CPP/OAS onset + drawdown.

Issue #294: The projection had no retirement transition. Employment income grew
at salary_growth forever, CPP/OAS (cpp_monthly_estimated / oas) were never
consumed, and registered-account drawdown never started.

This module owns the *transition* arithmetic (DP#10: Canada-owned retirement
rules) as small pure functions (DP#3: same inputs → same outputs) so the core
engine (simulation.py / simulation_state.py) can stay jurisdiction-agnostic and
simply call into here.

Design (first increment):
  - retirement_age: a per-member input (default 65). At/after a member's
    retirement age (within the projection horizon) that member's employment
    income stops (no salary, no further salary growth).
  - CPP starts from the member's `cpp_monthly_estimated` × 12, age-adjusted for
    `cpp_start_age` (+0.7%/mo after 65, −0.6%/mo before 65, same factors as
    retirement.cpp_benefit). CPP only begins once the member has reached
    cpp_start_age.
  - OAS starts from the full annual OAS maximum (retirement.oas_annual_max),
    age-adjusted for `oas_defer_months` (+0.6%/mo deferral bonus), minus the
    15% clawback above retirement.oas_clawback_threshold. OAS only begins once
    the member has reached oas_start_age (= 65 + oas_defer_months/12, floored
    at 65).
  - Drawdown (issue #301): sized to the *shortfall* after other retirement
    income. The target is a NET spending level — either an absolute
    `retirement.spending_target` or `retirement.net_replacement_rate` (default
    0.65) × pre-retirement NET income — and CPP/OAS/pension/remaining employment
    income are netted off first. The remaining net need is grossed up for the
    drawdown's tax so the after-tax proceeds meet the target. Drawn per
    `retirement.drawdown_order`.

References:
    countries/canada/retirement.py — OAS/CPP/RRIF primitives (clawback, age factors)
    countries/canada/docs/GOVERNMENT_REFERENCES.md — OAS, CPP/QPP entries
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

# Age-adjustment factors (mirror countries.canada.retirement constants so the
# transition stays consistent with the existing CPP primitives).
CPP_EARLY_START_PENALTY = 0.006   # 0.6% per month before 65 (max 36% at 60)
CPP_LATE_START_BONUS = 0.007      # 0.7% per month after 65 (max 42% at 70)
OAS_DEFER_BONUS = 0.006           # 0.6% per month of OAS deferral (max 36% at 70)

# Default per-member retirement age when the input omits it.
DEFAULT_RETIREMENT_AGE = 65

# OAS 15% recovery-tax (clawback) rate above the annual threshold — the
# statutory rate mirrored from countries.canada.retirement.OAS_CLAWBACK_RATE so
# the drawdown fold-in (issue #363 PR 2) and the standalone clawback primitive
# stay one number.
OAS_RECOVERY_RATE = 0.15

# Bounds for the OAS-clawback drawdown fixpoint (issue #363 PR 2). The draw
# raises income, income claws back OAS, the clawed OAS raises the net the draw
# must deliver, which raises the draw. The map
#     C -> clawback(other_taxable + oas_gross + taxable_draw(net_need + C))
# is a CONTRACTION for statutory brackets: its Lipschitz constant is
# ``OAS_RECOVERY_RATE x d(taxable_draw)/d(net_target)`` = ``0.15 / (1 - rate)``
# at the margin, which is <= 0.15 / (1 - 0.5331) ~= 0.32 < 1 for the top
# combined federal+provincial rate, and is exactly FLAT (Lipschitz 0) both
# below the threshold and once OAS is fully clawed back (the ``min``/``max``
# saturate). A contraction on the bounded interval ``[0, oas_gross]`` has a
# UNIQUE fixed point and converges geometrically, so a fixed iteration cap
# reaches it deterministically; ``_CLAWBACK_TOL`` makes the result independent
# of the cap once converged (the function stays pure — DP#26). Non-convergence
# within the cap is a hard error, never a silently under-resolved draw
# (AGENTS.md: prefer the loud failure over a plausible wrong number).
_MAX_CLAWBACK_ITERS = 64
_CLAWBACK_TOL = 1e-7

# Default fraction of pre-retirement NET income to target in retirement
# (issue #301). Retirees typically need ~50–70% of pre-retirement net income —
# savings, income tax, payroll deductions, the (paid-off) mortgage and child
# costs no longer recur. Configurable via retirement.net_replacement_rate; an
# absolute retirement.spending_target overrides it.
DEFAULT_NET_REPLACEMENT_RATE = 0.65


def member_age(birth_year: int, sim_year: int) -> int:
    """Age (in completed years) of a member during calendar year `sim_year`."""
    if not birth_year:
        return 0
    return sim_year - birth_year


def is_retired(birth_year: int, retirement_age: int, sim_year: int) -> bool:
    """True when the member has reached their retirement age by `sim_year`.

    A member with no birth_year (0) never retires within the projection — we
    cannot date the transition, so behavior stays identical to the pre-#294
    accumulation model (employment income continues).
    """
    if not birth_year:
        return False
    return member_age(birth_year, sim_year) >= retirement_age


def cpp_from_estimate(monthly_estimate: float, start_age: int,
                      claim_age: int) -> float:
    """Annual CPP from a member's monthly estimate, age-adjusted for start_age.

    The member's `cpp_monthly_estimated` is, by convention, the estimate *at 65*.
    Starting earlier reduces it (−0.6%/mo), later increases it (+0.7%/mo). CPP
    only flows once the member is at least `start_age`, so `claim_age` gates it.

    Args:
        monthly_estimate: cpp_monthly_estimated (estimate at 65).
        start_age: cpp_start_age (60–70).
        claim_age: member's current age this year.

    Returns:
        Annual CPP for this year (0 before the member reaches start_age).
    """
    if monthly_estimate <= 0 or claim_age < start_age:
        return 0.0
    annual_at_65 = monthly_estimate * 12
    start_age = max(60, min(70, start_age))
    if start_age < 65:
        months_early = (65 - start_age) * 12
        return annual_at_65 * (1 - months_early * CPP_EARLY_START_PENALTY)
    if start_age > 65:
        months_late = (start_age - 65) * 12
        return annual_at_65 * (1 + months_late * CPP_LATE_START_BONUS)
    return annual_at_65


def oas_gross(oas_annual_max: float, defer_months: int) -> float:
    """Gross annual OAS (before clawback), adjusted for deferral months."""
    if oas_annual_max <= 0:
        return 0.0
    defer_months = max(0, min(60, defer_months))  # cap deferral at 60 months (age 70)
    return oas_annual_max * (1 + defer_months * OAS_DEFER_BONUS)


def oas_after_clawback(oas_gross_amount: float, net_income: float,
                       clawback_threshold: float,
                       recovery_rate: float = 0.15) -> float:
    """Net OAS after the 15% recovery tax (clawback) above the threshold."""
    if oas_gross_amount <= 0:
        return 0.0
    if net_income <= clawback_threshold:
        return oas_gross_amount
    excess = net_income - clawback_threshold
    clawback = min(oas_gross_amount, excess * recovery_rate)
    return oas_gross_amount - clawback


# ── Retirement spending target & drawdown gross-up (issue #301) ──────────────

def retirement_spending_target(pre_retirement_net_income: float,
                               net_replacement_rate: float = DEFAULT_NET_REPLACEMENT_RATE,
                               spending_target: Optional[float] = None) -> float:
    """Annual NET spending the retiree targets (issue #301).

    Either an absolute ``spending_target`` (takes precedence when set > 0) or
    ``net_replacement_rate`` × pre-retirement NET income. This replaces the old
    gross-income proxy, which sized drawdown to reproduce ~$450k gross working
    income in retirement (unrealistic — see #301).

    Args:
        pre_retirement_net_income: combined household net (after-tax) income in
            the working years.
        net_replacement_rate: fraction of pre-retirement net income to target.
        spending_target: absolute annual net spending; overrides the rate.

    Returns:
        Target annual net spending (>= 0).
    """
    if spending_target and spending_target > 0:
        return float(spending_target)
    return max(0.0, pre_retirement_net_income) * max(0.0, net_replacement_rate)


@dataclass
class MemberRetirementIncome:
    """Per-member government retirement income for one year."""
    cpp: float = 0.0
    oas: float = 0.0
    pension: float = 0.0
    retired: bool = False

    @property
    def total(self) -> float:
        return self.cpp + self.oas + self.pension


def member_retirement_income(member: Dict, sim_year: int,
                             oas_annual_max: float,
                             oas_clawback_threshold: float,
                             other_net_income: float = 0.0) -> MemberRetirementIncome:
    """Compute a member's CPP + OAS + pension for `sim_year`.

    Government benefits only flow once the member is retired (>= retirement_age)
    AND has reached the relevant claim age (cpp_start_age / oas_start_age).

    Args:
        member: family member dict (birth_year, cpp_*, oas_*, pension_*, retirement_age).
        sim_year: calendar year.
        oas_annual_max: full annual OAS max for the year (retirement.oas_annual_max).
        oas_clawback_threshold: OAS recovery-tax threshold for the year.
        other_net_income: the member's other net income used to size the OAS
            clawback (CPP + pension + drawdown attributed to this member).

    Returns:
        MemberRetirementIncome with cpp/oas/pension and the retired flag.
    """
    birth_year = member.get('birth_year', 0)
    retirement_age = member.get('retirement_age', DEFAULT_RETIREMENT_AGE)
    retired = is_retired(birth_year, retirement_age, sim_year)
    if not retired:
        return MemberRetirementIncome(retired=False)

    age = member_age(birth_year, sim_year)

    # DP#32 (#606): cpp_start_age / oas_start_age of 0 is a data bug (never a
    # legitimate claim age -- CPP/OAS claim ages run 60-70), not "unset". Only
    # a genuinely ABSENT key falls back to 65; an explicit erroneous 0 must
    # surface (cpp_from_estimate clamps it into [60,70] rather than silently
    # reading as "start at 65").
    cpp_start_age = member.get('cpp_start_age')
    cpp_start_age = 65 if cpp_start_age is None else cpp_start_age
    cpp = cpp_from_estimate(
        monthly_estimate=member.get('cpp_monthly_estimated', 0) or 0,
        start_age=cpp_start_age,
        claim_age=age,
    )

    oas_start_age = member.get('oas_start_age')
    oas_start_age = 65 if oas_start_age is None else oas_start_age
    defer_months = member.get('oas_defer_months', 0) or 0
    if age >= oas_start_age:
        gross = oas_gross(oas_annual_max, defer_months)
        oas_net = oas_after_clawback(
            gross,
            net_income=other_net_income + cpp,
            clawback_threshold=oas_clawback_threshold,
        )
    else:
        oas_net = 0.0

    pension = member.get('pension_income_annual', 0) or 0

    return MemberRetirementIncome(cpp=cpp, oas=oas_net, pension=pension, retired=True)


# ── Drawdown ─────────────────────────────────────────────────────────────────

# Map a drawdown_order token to the jurisdiction_state['canada'] balance keys it
# draws from. Each token may span multiple accounts (e.g. both spouses' TFSA).
# 'rrsp_bracket_fill' (issue #618) draws the SAME accounts as 'rrsp' but is
# capped each year at the household's remaining headroom to `bracket_target`
# (see plan_drawdown_net) instead of drawing without limit.
_DRAWDOWN_SOURCES: Dict[str, List[str]] = {
    'tfsa': ['tfsa_primary_balance', 'tfsa_spouse_balance'],
    'non_reg': ['non_reg_balance'],          # special-cased (top-level on SimState)
    'rrsp': ['rrsp_balance', 'spousal_rrsp_balance', 'spouse_rrsp_balance'],
    'rrsp_bracket_fill': ['rrsp_balance', 'spousal_rrsp_balance', 'spouse_rrsp_balance'],
    'lif': ['lif_balance'],
    'lira': ['lira_balance'],
    'fhsa': ['fhsa_balance'],
}

# Accounts whose withdrawals are taxable as ordinary income (RRSP/RRIF/LIF/LIRA).
_TAXABLE_SOURCES = {'rrsp', 'rrsp_bracket_fill', 'lif', 'lira'}

# Tokens whose combined draw this year is capped to the household's headroom
# below `bracket_target` (issue #618 bracket-filling drawdown order).
_BRACKET_CAPPED_SOURCES = {'rrsp_bracket_fill'}

# Issue #1002: the 'lif' token's discretionary gross draw is capped at the LIF
# statutory maximum-withdrawal ceiling for the year (the annual cap on TOTAL LIF
# withdrawals, less the forced minimum already taken -- passed in as
# `lif_max_withdrawal`). A statutory per-jurisdiction cap (DP#10:
# locked_in_account.py owns LIF rules); the forced-minimum path already enforces
# it on its own slice, this caps the DISCRETIONARY slice so the two together
# cannot over-draw the LIF.
_LIF_CAPPED_SOURCES = {'lif'}

# Issue #363 PR 4: which spouse a taxable balance key is attributed to when the
# draw is split across the two spouses' SEPARATE bracket sets (Canada has no
# joint filing). Each key's taxable slice is then priced against — and claws
# back OAS from — that owner alone. The primary's own RRSP is the primary's; the
# spousal RRSP (contributed by the primary, but withdrawn by and taxed to the
# annuitant spouse) and the spouse's own RRSP are the spouse's — the same
# primary/spouse attribution ``apply_retirement_drawdown`` already books into
# ``spend_draw_primary_rrsp`` / ``spend_draw_spouse_rrsp``. The household's
# single non-registered / LIF / LIRA balances are not tracked per member, so
# they are attributed to the primary (the representative holder); TFSA/FHSA are
# tax-free and carry no bracket, so their owner is immaterial. This map is only
# consulted in the per-member split mode; the pre-PR-4 single-schedule mode
# routes every key to one 'household' owner.
_KEY_OWNER: Dict[str, str] = {
    'rrsp_balance': 'primary',
    'spousal_rrsp_balance': 'spouse',
    'spouse_rrsp_balance': 'spouse',
    'non_reg_balance': 'primary',
    'lif_balance': 'primary',
    'lira_balance': 'primary',
}


# ── Named drawdown-order candidates (issue #618) ────────────────────────────
# The optimizer's search dimension: each entry is a full withdrawal PRIORITY
# ORDER (not a single account), matching the shape `plan_drawdown_net`
# already consumes. 'tfsa_first' matches the pre-#618 hardcoded default
# (simulation.py) so re-running with this candidate reproduces today's
# baseline exactly.
DRAWDOWN_ORDER_CANDIDATES: List[Dict] = [
    {
        'id': 'tfsa_first',
        'label': 'TFSA-first (spend tax-free, preserve registered)',
        'order': ['tfsa', 'non_reg', 'rrsp', 'lif', 'lira'],
    },
    {
        'id': 'rrsp_meltdown',
        'label': 'RRSP-meltdown (spend registered first, preserve TFSA)',
        'order': ['rrsp', 'lif', 'lira', 'non_reg', 'tfsa'],
    },
    {
        'id': 'non_reg_first',
        'label': 'Non-registered-first (realize taxable investments first)',
        'order': ['non_reg', 'rrsp', 'lif', 'lira', 'tfsa'],
    },
    {
        'id': 'bracket_fill',
        'label': 'Bracket-filling (draw registered up to a target bracket, '
                 'then TFSA)',
        'order': ['rrsp_bracket_fill', 'tfsa', 'non_reg', 'rrsp', 'lif', 'lira'],
    },
]


@dataclass
class DrawdownResult:
    """Outcome of a one-year drawdown across registered/non-reg accounts."""
    total_withdrawn: float = 0.0
    taxable_withdrawn: float = 0.0      # portion taxable as ordinary income
    # Net (after-tax) dollars actually delivered toward the requested net
    # need. Equals the net need unless account balances ran out first (then
    # it is the amount actually deliverable, < net need).
    net_delivered: float = 0.0
    # Issue #363 PR 2: the OAS 15% recovery tax (clawback) the taxable draw
    # triggered above the threshold. The draw is grossed up to REPLACE it, so
    # ``net_delivered`` (after-income-tax proceeds) already includes this
    # amount and ``net_delivered - oas_clawback`` is the true spendable net.
    # The caller books it as reduced OAS income (single-count: the extra gross
    # drawn to cover it cancels against the OAS reduction in the cash-flow
    # identity). 0.0 when no OAS/threshold is supplied (clawback disabled).
    oas_clawback: float = 0.0
    # Issue #754: the realized capital gain (proceeds - ACB, at 100%) crystallized
    # by disposing of non-registered assets this draw -- ``gross_non_reg_drawn x
    # gain_frac``, where ``gain_frac`` is the accrued-gain fraction of the pot at
    # the moment of the draw. This is the RAW gain, BEFORE the capital-gains
    # inclusion; ``taxable_withdrawn`` already carries ``cg_inclusion`` x this
    # amount as the ordinary-income slice the draw is taxed on. Surfaced so
    # year-end consumers (the AMT assessment #710, ITA s.127.52(1)(d) reads the
    # gain at 100% inclusion) have a real realized-gain base to read rather than
    # re-deriving it from the bundled taxable total. 0.0 for a draw that touches
    # no non-reg, or a non-reg pot sitting at/below its ACB (gain_frac floored 0).
    realized_capital_gain: float = 0.0
    # Per-balance-key deltas to apply (negative = reduce balance).
    balance_deltas: Dict[str, float] = None
    # Issue #363 PR 4: the taxable draw recognized against each spouse's OWN
    # bracket stack, keyed by owner ('primary'/'spouse' in the per-member split,
    # or a single 'household' owner in the pre-PR-4 single-schedule mode). The
    # wrapper reads it to compute each spouse's OAS clawback on that spouse's own
    # income. Its values sum to ``taxable_withdrawn``.
    taxable_by_owner: Dict[str, float] = None
    # Issue #140: the ITA s.111(1)(b) net-capital-loss pool dollars (taxable
    # basis) this draw CONSUMED -- the sheltered slice of the non-reg draws'
    # taxable additions never entered taxable income. The caller books it
    # against ``ws.capital_loss_offset_used``; 0.0 when no pool was supplied.
    cg_loss_offset_used: float = 0.0

    def __post_init__(self):
        if self.balance_deltas is None:
            self.balance_deltas = {}
        if self.taxable_by_owner is None:
            self.taxable_by_owner = {}


def _bracket_at(income: float, brackets: List[Dict]):
    """``(marginal_rate, ceiling)`` for the NEXT taxable dollar above ``income``.

    At an exact bracket boundary the *higher* bracket wins — the next dollar is
    taxed there — so a slice priced by walking these tuples reproduces
    ``tax_calculator.tax_on_income`` to the cent (the same convention that
    function integrates under). The top (unbounded) bracket returns an infinite
    ceiling.
    """
    for b in brackets[:-1]:
        upper = b['max'] if b['max'] else float('inf')
        if income < upper:
            return b['rate'], upper
    # Top bracket: a falsy `max` is the CRA unbounded ceiling (float('inf')),
    # so income at or above every finite boundary falls here.
    last = brackets[-1]
    return last['rate'], (last['max'] if last['max'] else float('inf'))


def _price_source_draw(running_income: float, inclusion: float,
                       net_target: float, max_gross: float,
                       brackets: Optional[List[Dict]],
                       flat_rate: float,
                       taxable_shelter: float = 0.0):
    """Gross to withdraw from one source to deliver up to ``net_target`` net.

    ``inclusion`` is the fraction of each gross dollar that is *taxable income*:
    ``1.0`` for RRSP/RRIF/LIF/LIRA, ``gain_frac·cg_inclusion`` for non-reg,
    ``0.0`` for TFSA/FHSA. ``running_income`` is the taxable income the draw
    stacks on top of (household CPP/pension plus whatever taxable draw has
    already been recognized this year).

    With ``brackets`` (issue #363) each taxable dollar is priced at its REAL
    marginal cost as it climbs the table: the incremental tax on a slice is
    ``tax_on_income(running_income + slice) − tax_on_income(running_income)``,
    solved per-source by the closed-form bracket walk below (the same
    bracket-fill shape as ``tax_calculator.deduction_value``). Without
    ``brackets`` it falls back to the deprecated single flat ``flat_rate`` —
    #579's residual, kept so the signature stays additive.

    ``taxable_shelter`` (issue #140): taxable-basis dollars of net-capital-loss
    carry-forward (ITA s.111(1)(b)) still available to this draw. The LEAD
    slice of the gross -- whose entire taxable addition the pool absorbs -- is
    genuinely tax-free and delivers $1 net per dollar WITHOUT STACKING on
    ``running_income`` (the sheltered slice never enters taxable income, so
    every later slice re-brackets from the same base). Solving it as a lead
    slice (rather than post-adjusting a fully-taxed solve) keeps the gross-up
    EXACT: the household draws no more gross than the now-lower tax requires.
    ``0.0`` (default) takes the untouched pre-#140 path byte-for-byte (DP#32).

    Returns ``(gross_drawn, net_delivered)`` with ``gross_drawn ≤ max_gross``
    and ``net_delivered ≤ net_target``.
    """
    if net_target <= 0 or max_gross <= 0:
        return 0.0, 0.0
    # Issue #140: price the sheltered lead slice first. Each of its gross
    # dollars carries ``inclusion`` taxable that the loss pool absorbs, so it
    # delivers $1 net and adds nothing to taxable income. The remainder (if
    # any need/balance/leftover pool) recurses with the shelter consumed.
    if taxable_shelter > 0.0 and inclusion > 0.0:
        shelter_gross = min(taxable_shelter / inclusion, max_gross,
                            float(net_target))
        if shelter_gross > 0.0:
            rest_gross, rest_net = _price_source_draw(
                running_income, inclusion, net_target - shelter_gross,
                max_gross - shelter_gross, brackets, flat_rate)
            return shelter_gross + rest_gross, shelter_gross + rest_net
    if inclusion <= 0.0:
        # Tax-free: $1 gross delivers $1 net.
        gross = min(net_target, max_gross)
        return gross, gross
    if not brackets:
        # Deprecated flat fallback (#579): one rate for the whole draw.
        rate = max(0.0, min(0.95, flat_rate))
        net_per = max(0.01, 1.0 - inclusion * rate)
        gross = min(net_target / net_per, max_gross)
        return gross, gross * net_per
    # Progressive re-bracketing (#363): walk the brackets, pricing each taxable
    # slice at the marginal rate of the income band it actually lands in.
    gross = 0.0
    net_done = 0.0
    income = running_income
    while net_done < net_target and gross < max_gross:
        rate, ceiling = _bracket_at(income, brackets)
        rate = max(0.0, min(0.95, rate))
        net_per = max(0.01, 1.0 - inclusion * rate)
        # Gross that lifts taxable income to this bracket's ceiling.
        if ceiling == float('inf'):
            gross_to_ceiling = float('inf')
        else:
            gross_to_ceiling = (ceiling - income) / inclusion
        gross_cap = min(gross_to_ceiling, max_gross - gross)
        net_cap = gross_cap * net_per
        if net_target - net_done <= net_cap:
            gross += (net_target - net_done) / net_per
            net_done = net_target
            break
        gross += gross_cap
        net_done += net_cap
        income += gross_cap * inclusion
        # Loop guard (gross < max_gross) handles balance exhaustion; when the
        # last slice fills max_gross the while-condition ends the walk.
    return gross, net_done


def _draw_sources_to_net(net_target: float, drawdown_order: List[str],
                         canada: Dict, non_reg_balance: float,
                         flat_rate: float, gain_frac: float, cg_inclusion: float,
                         brackets: Optional[List[Dict]],
                         owner_specs: Dict[str, Dict],
                         owner_of,
                         lif_max_withdrawal: Optional[float] = None,
                         cg_loss_offset: float = 0.0) -> DrawdownResult:
    """Fill ``net_target`` (after-income-tax) account by account — the pure
    per-source waterfall extracted from ``plan_drawdown_net`` so the OAS-clawback
    fixpoint can re-drive it with a grossed-up target (issue #363 PR 2). Returns
    a fresh ``DrawdownResult`` with no clawback term; the wrapper owns that.

    Issue #363 PR 4: the taxable draw is split across the spouses' SEPARATE
    bracket stacks. ``owner_specs`` maps each owner key ('primary'/'spouse', or a
    single 'household' owner in the pre-PR-4 single-schedule mode) to its
    immutable ``other_taxable_income`` / ``bracket_target`` / ``bracket_fill_base``;
    ``owner_of(balance_key)`` returns the owner a taxable balance is attributed
    to. Each owner carries its OWN running taxable income (seeded from its other
    income, grown as its own slices are recognized) so a slice re-brackets
    against that spouse's income alone — never the couple's combined stack. The
    per-owner recognized taxable is returned in ``result.taxable_by_owner`` for
    the wrapper's per-spouse OAS-clawback fold.
    """
    result = DrawdownResult()
    remaining = net_target
    # Issue #140: the still-available slice of the caller's net-capital-loss
    # pool, depleted as the non-reg draws' taxable additions are sheltered.
    cg_loss_remaining = cg_loss_offset
    # Per-owner MUTABLE running taxable income (issue #363/#618/PR 4). Seeded
    # from each owner's other taxable income (CPP/pension) and grown as that
    # owner's taxable slices are recognized, so successive draws re-bracket at
    # the right rate against that spouse's income. Built fresh each call (pure).
    running_income = {k: spec['other_taxable_income'] for k, spec in owner_specs.items()}
    taxable_by_owner = {k: 0.0 for k in owner_specs}

    for token in drawdown_order:
        if remaining <= 0:
            break
        keys = _DRAWDOWN_SOURCES.get(token)
        if not keys:
            continue
        taxable = token in _TAXABLE_SOURCES

        # Issue #618/PR 4: per-owner headroom for a bracket-capped token. The
        # base the headroom fills against is that owner's already-recognized
        # TAXABLE income INCLUDING OAS (`bracket_fill_base`) — OAS is taxable
        # income for bracket purposes, even though it is RECEIVED net of the 15%
        # recovery tax, so the RRSP draw must not "fill" the room OAS already
        # occupies. When no OAS-inclusive base is supplied (None — a caller not
        # opting into #618, e.g. a direct unit test) fall back to that owner's
        # other_taxable_income, preserving the pre-#618 ceiling. Each owner's
        # keys (e.g. the spouse's spousal + own RRSP) share ONE per-owner
        # ceiling; before PR 4 all keys shared the single 'household' ceiling.
        bracket_capped = token in _BRACKET_CAPPED_SOURCES
        token_headroom = {}          # owner -> remaining room (only when capped)
        if bracket_capped:
            for owner_key, spec in owner_specs.items():
                bt = spec['bracket_target']
                if bt is None:
                    continue
                base = (spec['other_taxable_income']
                        if spec['bracket_fill_base'] is None
                        else spec['bracket_fill_base'])
                token_headroom[owner_key] = max(0.0, bt - base)

        # Issue #1002: the 'lif' token's discretionary gross draw is capped at
        # the LIF statutory maximum-withdrawal ceiling for the year. The ceiling
        # is a SINGLE household-level dollar amount (one LIF, attributed to the
        # primary) -- the caller passes the DISCRETIONARY room
        # (``max_withdrawal - forced_minimum_already_taken``) so the forced min
        # (apply_lira_lif) + this discretionary draw never exceed the annual
        # statutory cap. ``None`` (a direct unit-test caller not opting in)
        # disables the cap entirely, preserving the pre-#1002 path (DP#13:
        # None disables, never a hardcoded opinion). A 0.0 ceiling means the LIF
        # has no discretionary room this year (min == max, or no LIF activity) --
        # a hard zero that caps the draw at nothing, NOT a fallback (DP#32).
        lif_capped = token in _LIF_CAPPED_SOURCES and lif_max_withdrawal is not None
        lif_headroom = lif_max_withdrawal if lif_capped else None

        for key in keys:
            if remaining <= 0:
                break
            owner_key = owner_of(key)
            capped = bracket_capped and owner_key in token_headroom
            if capped and token_headroom[owner_key] <= 0:
                continue  # no room left in this owner's target bracket this year
            if lif_capped and lif_headroom <= 0:
                # The LIF's discretionary room is exhausted for the year; stop
                # drawing from it and let the residual shortfall fall through to
                # the NEXT token in drawdown_order (do not silently under-deliver
                # the net target -- the waterfall continues, issue #1002).
                break
            if key == 'non_reg_balance':
                available = non_reg_balance + result.balance_deltas.get(key, 0.0)
            else:
                available = (canada.get(key, 0) or 0) + result.balance_deltas.get(key, 0.0)
            if available <= 0:
                continue
            # Fraction of each gross dollar that is taxable income: fully
            # taxable RRSP/RRIF/LIF/LIRA, the accrued-gain slice for non-reg,
            # nothing for TFSA/FHSA.
            if taxable:
                inclusion = 1.0
            elif key == 'non_reg_balance':
                inclusion = gain_frac * cg_inclusion
            else:
                inclusion = 0.0
            max_gross = available
            if capped:
                max_gross = min(max_gross, token_headroom[owner_key])
            if lif_capped:
                max_gross = min(max_gross, lif_headroom)
            # available > 0 and remaining > 0 above, so _price_source_draw
            # always returns a positive gross here (tax-free delivers
            # min(need, available); flat/progressive accumulate a positive
            # first slice) — no zero-take guard is reachable.
            take, net_delivered = _price_source_draw(
                running_income[owner_key], inclusion, remaining, max_gross,
                brackets, flat_rate, taxable_shelter=cg_loss_remaining)
            result.balance_deltas[key] = result.balance_deltas.get(key, 0.0) - take
            result.total_withdrawn += take
            taxable_added = take * inclusion
            result.taxable_withdrawn += taxable_added
            # Issue #754: crystallize the realized capital gain on the non-reg
            # slice -- the RAW gain (proceeds x gain_frac), before inclusion. The
            # taxable slice above is cg_inclusion x this; here we surface the
            # 100% figure the AMT base (#710) reads. gain_frac is already floored
            # at 0 by the caller, so a pot at/below ACB contributes nothing.
            if key == 'non_reg_balance':
                result.realized_capital_gain += take * gain_frac
                # Issue #140: the lead slice the s.111(1)(b) pool just
                # sheltered -- priced INSIDE the solver above (its taxable
                # addition never entered ``running_income``, so later slices
                # re-bracket from the same base). Book the consumption so the
                # fold's capital_loss_carryforward rule can reconcile the
                # pool. With no pool available every adjustment here is an
                # exact float no-op on the pre-#140 values (DP#32).
                sheltered = min(cg_loss_remaining, taxable_added)
                cg_loss_remaining -= sheltered
                result.cg_loss_offset_used += sheltered
                taxable_added -= sheltered
                result.taxable_withdrawn -= sheltered
            running_income[owner_key] += taxable_added
            taxable_by_owner[owner_key] += taxable_added
            remaining -= net_delivered
            if capped:
                token_headroom[owner_key] -= take
            if lif_capped:
                lif_headroom -= take

    result.net_delivered = net_target - max(0.0, remaining)
    result.taxable_by_owner = taxable_by_owner
    return result


def plan_drawdown_net(net_need: float, drawdown_order: List[str],
                      canada: Dict, non_reg_balance: float,
                      non_reg_acb: float, marginal_rate: float,
                      cg_inclusion: float = 0.5,
                      bracket_target: Optional[float] = None,
                      other_taxable_income: float = 0.0,
                      brackets: Optional[List[Dict]] = None,
                      bracket_fill_base: Optional[float] = None,
                      oas_gross: float = 0.0,
                      oas_clawback_threshold: Optional[float] = None,
                      oas_recovery_rate: float = OAS_RECOVERY_RATE,
                      per_member: Optional[Dict[str, Dict]] = None,
                      lif_max_withdrawal: Optional[float] = None,
                      cg_loss_offset: float = 0.0) -> DrawdownResult:
    """Withdraw to hit a NET (after-tax) spending target, per-source tax-aware.

    This is the only drawdown model (#579 — the old blended-rate ``gross``
    path over-drew whenever the lead sources were tax-free and has been
    deleted). It fills the *net* need account by account, grossing up
    **only** the taxable portion of each source:

      - TFSA / FHSA: tax-free — $1 withdrawn delivers $1 net.
      - Non-registered: only the accrued-gain fraction is taxable (at
        ``cg_inclusion``), so $1 delivers ``1 − gain_frac·incl·rate`` net.
      - RRSP / RRIF / LIF / LIRA: fully taxable — $1 delivers ``1 − rate`` net.

    The extra gross drawn to cover tax leaves the account (it is remitted to
    CRA), sized to the *actual* tax on each source rather than the whole
    need. This removes the systematic over-draw that made early retirement
    (living off tax-free TFSA/non-reg) look far more expensive than it is.

    Issue #363 (progressive re-bracketing, PR 1): when ``brackets`` is supplied
    the taxable portion of each draw is priced at its REAL marginal cost as it
    climbs the table — the incremental tax on a slice is
    ``tax_on_income(before + slice) − tax_on_income(before)``, where ``before``
    is ``other_taxable_income`` plus whatever taxable draw has already been
    recognized this year — instead of one flat rate for the whole draw. The
    scalar ``marginal_rate`` remains a deprecated fallback used only when
    ``brackets`` is absent, so the signature stays additive.

    Issue #363 (OAS clawback, PR 2): when ``oas_gross > 0`` and
    ``oas_clawback_threshold`` is supplied, the 15% OAS recovery tax is folded
    into the draw. Each taxable draw dollar that lifts net income
    (``other_taxable_income + oas_gross + taxable_draw``) above the threshold
    claws back 15 cents of OAS, so in the clawback band the marginal cost of an
    RRSP dollar is its tax rate PLUS 0.15. The draw is grossed up to REPLACE the
    clawed OAS, so the household still nets ``net_need``. This is a genuine
    fixpoint (draw → income → clawback → net the draw must deliver → draw); it is
    resolved by a BOUNDED, DETERMINISTIC iteration of the target
    ``net_need + clawback`` (see ``_MAX_CLAWBACK_ITERS`` for the contraction
    argument). ``DrawdownResult.net_delivered`` is the after-income-tax proceeds
    ``net_need + oas_clawback``; ``oas_clawback`` is the recovery tax the caller
    books as reduced OAS income (single-count — the extra gross cancels the OAS
    reduction in the cash-flow identity), and ``net_delivered - oas_clawback`` is
    the true spendable ``net_need``.

    Issue #363 (per-spouse split, PR 4): when ``per_member`` is supplied the
    draw is split across the two spouses' SEPARATE bracket sets (Canada has no
    joint filing). Each taxable balance key is attributed to an owner
    (``_KEY_OWNER``: the primary's own RRSP to the primary; the spousal and the
    spouse's own RRSP to the spouse; the household's single non-reg/LIF/LIRA to
    the primary as representative holder), and its taxable slice re-brackets
    against — and claws back OAS from — THAT spouse's income alone (their own
    ``other_taxable_income``, ``oas_gross``, ``bracket_target`` and
    ``bracket_fill_base``), never the couple's combined stack. The household net
    need stays a SINGLE pooled target the waterfall fills exactly, so money
    conservation is unchanged; only the tax PRICING of each drawn slice is
    per-spouse. Splitting income across two lower bracket sets is weakly
    tax-reducing (each slice sits on a base no higher than the combined one), so
    the split draw is <= the single-schedule draw for the same net. When
    ``per_member`` is None the draw is priced against ONE 'household' schedule
    exactly as before (the pre-PR-4 path, used by direct callers that pass only
    the scalar ``other_taxable_income`` / ``oas_gross`` / ``bracket_*`` args).

    Issue #618 — bracket-filling: when ``drawdown_order`` contains the
    ``'rrsp_bracket_fill'`` token, the combined RRSP/RRIF draw against that
    token is capped at ``max(0, bracket_target - bracket_fill_base)`` — the
    household's remaining room before crossing into the next bracket — rather
    than being drawn without limit. ``bracket_fill_base`` is the household's
    already-recognized TAXABLE income INCLUDING the full OAS: OAS is taxable
    income for bracket purposes even though it is received net of the 15%
    recovery tax, so the draw must not "fill" the room OAS already occupies
    (without this the bracket-fill order over-draws by the OAS slice). When
    ``bracket_fill_base is None`` the base falls back to
    ``other_taxable_income`` (the pre-#618 OAS-excluded ceiling), so callers
    that don't opt in are unaffected. Any of the net need that the cap leaves
    unmet falls through to the next token in ``drawdown_order`` (e.g. TFSA).

    Args:
        net_need: after-tax spending shortfall to cover (>= 0).
        drawdown_order: ordered source tokens.
        canada: jurisdiction_state['canada'] (read-only).
        non_reg_balance: current non-reg balance (top-level).
        non_reg_acb: non-reg adjusted cost base (for the taxable gain fraction).
        marginal_rate: DEPRECATED flat fallback (#579) — the retiree's marginal
            rate on taxable withdrawals (0–<1), used only when ``brackets`` is
            absent. When ``brackets`` is supplied the draw is re-bracketed
            progressively (#363) and this scalar is ignored.
        cg_inclusion: capital-gains inclusion rate (default 0.5).
        bracket_target: income ceiling for the ``'rrsp_bracket_fill'`` token
            (DP#13: ``None`` disables the cap entirely, not a hardcoded
            opinion — the caller supplies a year-versioned bracket boundary).
        other_taxable_income: the household's taxable income already
            recognized this year from other sources (CPP/pension), used to size
            the ``rrsp_bracket_fill`` headroom, as the running base the
            progressive draw stacks on (#363), AND as the non-OAS part of the
            clawback base (PR 2).
        brackets: combined year-versioned tax brackets (DP#20). When supplied,
            the taxable draw is re-bracketed progressively (#363); when ``None``
            the deprecated flat ``marginal_rate`` prices the whole draw (#579).
        bracket_fill_base: the OAS-INCLUSIVE taxable base the
            ``'rrsp_bracket_fill'`` headroom fills against (#618): CPP +
            pension + full OAS. ``None`` (the default) falls back to
            ``other_taxable_income`` — the pre-#618 OAS-excluded ceiling — so
            it only affects the headroom cap, never the progressive tax base
            or the clawback base (both of which keep OAS out of
            ``other_taxable_income`` and add ``oas_gross`` separately, so
            threading OAS here does not double-count it).
        oas_gross: gross annual OAS subject to the recovery tax (0.0 disables
            the clawback fold — DP#32: a genuine zero-OAS household, not a
            fallback). In the single-schedule mode this is combined across
            members; in the ``per_member`` split (PR 4) each spouse's own gross
            OAS drives that spouse's own clawback.
        oas_clawback_threshold: OAS recovery-tax threshold for the year
            (``None`` disables the clawback fold — the caller supplies a
            year-versioned value, DP#20). Shared across spouses (the statutory
            threshold is per-person but identical each year).
        oas_recovery_rate: OAS recovery-tax rate (statutory 15%).
        per_member: issue #363 PR 4. ``None`` (default) prices the whole draw
            against ONE 'household' schedule (the pre-PR-4 behaviour, driven by
            the scalar ``other_taxable_income`` / ``oas_gross`` / ``bracket_*``
            args above). When supplied it is ``{'primary': {...}, 'spouse':
            {...}}`` where each spouse's dict carries its OWN
            ``other_taxable_income``, ``oas_gross``, ``bracket_target`` and
            ``bracket_fill_base``; the draw is then split across the two
            spouses' separate bracket sets (see ``_KEY_OWNER``). The scalar args
            are ignored for the split's tax base when ``per_member`` is present.
        lif_max_withdrawal: issue #1002. The LIF statutory
            maximum-withdrawal ceiling for the year -- the DISCRETIONARY room
            left after the forced minimum (apply_lira_lif) already took its
            statutory slice, i.e. ``fund.maximum_withdrawal(year) -
            lif_withdrawal_already_taken``. The ``'lif'`` token's gross draw is
            capped at this amount so the forced minimum + discretionary draw
            never exceed the annual statutory cap (the forced path already
            enforces the cap on its own slice; this caps the remainder). ``None``
            (default -- a direct unit-test caller not opting in) disables the
            cap entirely, preserving the pre-#1002 path (DP#13: None disables,
            never a hardcoded opinion). A ``0.0`` ceiling is a HARD zero (the
            LIF has no discretionary room this year -- min == max, or no LIF
            activity): the ``'lif'`` draw is capped at nothing and the residual
            shortfall falls through to the next token in ``drawdown_order``
            (DP#32: zero is a value, not a fallback). Reuses the same
            ``fund.maximum_withdrawal`` primitive the forced path uses (DP#10:
            locked_in_account.py owns LIF rules).
        cg_loss_offset: issue #140. The ITA s.111(1)(b) net-capital-loss
            carry-forward still available this year (TAXABLE basis -- the
            opening pool less what earlier rules already consumed). Each
            non-reg draw's LEAD taxable slice is sheltered dollar-for-dollar
            from it -- priced inside ``_price_source_draw`` so the gross-up
            stays exact (the sheltered dollars deliver $1 net, never enter
            taxable income, and later slices re-bracket from the same base).
            The amount consumed is reported on ``result.cg_loss_offset_used``
            for the fold's ``capital_loss_carryforward`` rule to reconcile.
            ``0.0`` (default) takes the untouched pre-#140 path byte-for-byte
            (DP#32).

    Returns:
        DrawdownResult with gross/taxable withdrawn, per-key balance deltas, and
        (PR 2) the ``oas_clawback`` the draw triggered.
    """
    if net_need <= 0:
        return DrawdownResult()
    flat_rate = max(0.0, min(0.95, marginal_rate))
    gain_frac = ((non_reg_balance - non_reg_acb) / non_reg_balance
                 if non_reg_balance > 0 else 0.0)
    gain_frac = max(0.0, min(1.0, gain_frac))

    # Issue #363 PR 4: resolve the owner schedule(s) the taxable draw prices
    # against. Single-schedule (per_member is None) routes every key to one
    # 'household' owner carrying the scalar args — byte-identical to the pre-PR-4
    # behaviour. The per-member split gives each spouse its own bracket stack,
    # OAS and headroom; the household net need stays a single pooled target.
    if per_member is None:
        owner_specs = {'household': {
            'other_taxable_income': other_taxable_income,
            'oas_gross': oas_gross,
            'bracket_target': bracket_target,
            'bracket_fill_base': bracket_fill_base,
        }}
        def owner_of(key):
            return 'household'
    else:
        owner_specs = {
            owner: {
                'other_taxable_income': spec.get('other_taxable_income', 0.0),
                'oas_gross': spec.get('oas_gross', 0.0),
                'bracket_target': spec.get('bracket_target'),
                'bracket_fill_base': spec.get('bracket_fill_base'),
            }
            for owner, spec in per_member.items()
        }
        # A key with no explicit owner (tax-free TFSA/FHSA) is immaterial to
        # tax; attribute it to any existing owner so its (zero-taxable) slice
        # has a stack to sit on. The primary always exists in the split.
        _default_owner = 'primary' if 'primary' in owner_specs else next(iter(owner_specs))
        def owner_of(key):
            return _KEY_OWNER.get(key, _default_owner)

    def _fill(net_target: float) -> DrawdownResult:
        return _draw_sources_to_net(
            net_target, drawdown_order, canada, non_reg_balance,
            flat_rate, gain_frac, cg_inclusion, brackets,
            owner_specs, owner_of, lif_max_withdrawal,
            cg_loss_offset=cg_loss_offset)

    # No OAS to claw back (no owner has OAS, or the caller did not supply a
    # threshold): the draw is exactly the PR-1 progressive fill of net_need.
    any_oas = any(spec['oas_gross'] > 0.0 for spec in owner_specs.values())
    if not (any_oas and oas_clawback_threshold is not None):
        return _fill(net_need)

    # OAS-clawback fixpoint (issue #363 PR 2, per-spouse in PR 4). ``clawback``
    # is the TOTAL recovery tax the draw must additionally replace — the sum of
    # each owner's own clawback, computed on THAT owner's own income (its other
    # taxable income + its gross OAS + its own recognized taxable draw). Each
    # pass re-drives the pooled draw to ``net_need + clawback`` and recomputes.
    # Bounded + deterministic: each owner's map is a contraction (see
    # _MAX_CLAWBACK_ITERS), so their sum is too.
    clawback = 0.0
    converged = False
    for _ in range(_MAX_CLAWBACK_ITERS):
        probe = _fill(net_need + clawback)
        new_clawback = 0.0
        for owner, spec in owner_specs.items():
            og = spec['oas_gross']
            if og <= 0.0:
                continue
            income = (spec['other_taxable_income'] + og
                      + probe.taxable_by_owner.get(owner, 0.0))
            new_clawback += min(
                og,
                max(0.0, income - oas_clawback_threshold) * oas_recovery_rate)
        converged = abs(new_clawback - clawback) <= _CLAWBACK_TOL
        clawback = new_clawback
        if converged:
            break
    if not converged:
        raise RuntimeError(
            f"OAS clawback fixpoint did not converge in {_MAX_CLAWBACK_ITERS} "
            f"iterations (last clawback {clawback:.6f}); refusing to return an "
            f"under-resolved draw (AGENTS.md: prefer the loud failure)")

    result = _fill(net_need + clawback)
    result.oas_clawback = clawback
    return result


def price_forced_rrif_tax(other_taxable_income: float, oas_gross: float,
                          prior_taxable_draw: float, forced_taxable: float,
                          brackets: Optional[List[Dict]],
                          oas_clawback_threshold: Optional[float],
                          oas_recovery_rate: float = OAS_RECOVERY_RATE,
                          flat_rate: float = 0.0):
    """Income tax + incremental OAS recovery tax on a FORCED, fixed taxable
    RRIF-minimum withdrawal (issue #825).

    The mandatory RRIF minimum (``apply_rrif_minimum``) forces out a FIXED
    taxable amount whose after-income-tax proceeds are reinvested in non-reg.
    Its tax used to be a flat placeholder marginal rate evaluated at a nominal
    ``$40,000`` slice, with the OAS clawback the minimum triggers omitted
    entirely. This prices it with the SAME machinery the discretionary drawdown
    uses (``plan_drawdown_net``) — no parallel model:

      - Progressive re-bracketing (#363 PR 1): the forced slice is priced as
        ``tax_on_income(base + forced) − tax_on_income(base)``, where ``base`` is
        this spouse's other taxable income (CPP/pension) plus the discretionary
        taxable draw ALREADY recognized this year — OAS kept out of the
        income-tax base exactly as ``plan_drawdown_net`` does (OAS is handled via
        the clawback below, not re-bracketed here).
      - OAS clawback (#363 PR 2): the INCREMENTAL 15% recovery tax the forced
        slice triggers, ``clawback(claw_base + forced) − clawback(claw_base)``,
        where the clawback base adds gross OAS (the PR-2 convention). Only the
        increment is returned — the discretionary draw already booked the
        clawback its own taxable draw caused (``apply_retirement_drawdown``).

    Unlike ``plan_drawdown_net`` this needs no clawback fixpoint: the forced
    amount is FIXED (not grossed up to hit a net target), so both terms are
    closed-form. Per-spouse pricing (#363 PR 4) is the caller's job — it invokes
    this once per spouse with that spouse's own bases.

    ``brackets`` absent falls back to the deprecated flat ``flat_rate`` (the
    retiree marginal rate), matching ``_price_source_draw``'s #579 residual;
    the live fold always supplies year-versioned brackets.

    Returns ``(income_tax, oas_clawback_increment)``, both ``>= 0``.
    """
    if forced_taxable <= 0:
        return 0.0, 0.0
    base = other_taxable_income + prior_taxable_draw
    if brackets:
        from tax_calculator import tax_on_income
        income_tax = (tax_on_income(base + forced_taxable, brackets)
                      - tax_on_income(base, brackets))
    else:
        income_tax = forced_taxable * max(0.0, min(0.95, flat_rate))
    oas_clawback = 0.0
    if oas_gross > 0.0 and oas_clawback_threshold is not None:
        claw_base = other_taxable_income + oas_gross + prior_taxable_draw
        before = min(oas_gross,
                     max(0.0, claw_base - oas_clawback_threshold) * oas_recovery_rate)
        after = min(oas_gross,
                    max(0.0, claw_base + forced_taxable - oas_clawback_threshold)
                    * oas_recovery_rate)
        oas_clawback = max(0.0, after - before)
    return max(0.0, income_tax), oas_clawback


@dataclass
class SmUnwindResult:
    """Outcome of unwinding a slice of the Smith-Manoeuvre sleeve in a
    liquidate-to-target decumulation year (issue #1017).

    The SM sleeve is a leveraged non-reg portfolio: selling it realizes a
    capital gain (taxed) or a capital loss (deductible against the year's
    other taxable income, issue #110); the proceeds repay the readvanceable
    HELOC that financed it, and the NET (after tax/credit + HELOC
    repayment) funds the spending shortfall the ordinary financial drawdown
    could not cover. Money-conserving:
    ``net_delivered == gross_sold - tax - heloc_repaid`` (with ``tax`` signed).
    """
    gross_sold: float = 0.0          # SM portfolio proceeds (FMV sold)
    tax: float = 0.0                 # capital-gains income tax on the realized gain (negative = deductible loss credit)
    heloc_repaid: float = 0.0        # SM HELOC principal repaid from the proceeds
    net_delivered: float = 0.0       # proceeds - tax - heloc_repaid (to spending)
    realized_gain: float = 0.0       # pre-inclusion realized gain (proceeds - ACB)


def price_sm_unwind(net_need: float, sm_fmv: float, sm_acb: float,
                     sm_heloc: float, brackets: Optional[List[Dict]],
                     other_income: float, inclusion_rate: float = 0.5,
                     flat_rate: float = 0.40) -> SmUnwindResult:
    """Price selling a slice of the SM sleeve to deliver ``net_need`` after
    capital-gains tax and a PROPORTIONAL repayment of the SM HELOC (issue #1017).

    The HELOC is repaid in proportion to the fraction of the sleeve sold: sell
    fraction ``f = gross_sold / sm_fmv`` of the portfolio, repay ``f * sm_heloc``
    of the loan. This couples the asset and its financing so both drain to zero
    together as the sleeve is unwound -- the leveraged position is closed out,
    not left as $520k of debt riding to death against a sold asset (DP#18:
    the overlay's money flow must conserve, and the HELOC principal the sale
    retires is a real liability reduction, not a free default).

    The realized gain/loss (``gross_sold * gain_frac``, where ``gain_frac`` is
    the sleeve's SIGNED accrued-gain fraction) is included at
    ``inclusion_rate`` and priced at its REAL marginal cost through the
    progressive ``brackets``, STACKING signed: a positive gain stacks on
    ``other_income`` (the household's already-recognized taxable income this
    year -- the discretionary drawdown + forced RRIF + CPP/pension) as extra
    taxable income, while a capital LOSS (``gain_frac < 0``, an underwater
    pot) offsets the same ``other_income`` as a DEDUCTIBLE loss priced through
    the identical bracket sweep -- mirroring ``liquidation_waterfall
    .capital_gains_cost``'s signed-and-unclamped ``gain_frac`` (issue #110;
    the prior ``max(0.0, gain_frac)`` floor silently swallowed an underwater
    pot's loss, leaving the surviving sleeve carrying ``acb > fmv`` with no
    loss booked anywhere). Solved by binary search because the progressive tax
    makes ``net(gross)`` non-linear; ``net(gross)`` is monotonic increasing on
    each linear tax band, so the search is well-posed.

    The loss's deductible slice (``inclusion_rate x gross_sold x gain_frac``)
    is priced against the year's ``other_income`` only. Under ITA s.111(1)(c)
    net-capital-loss rules the portion the year's other income cannot absorb
    must carry forward to shelter a later capital gain; this engine has no
    loss-carryforward state, so the unabsorbed remainder is neither credited
    nor persisted here (issue #140). See ``test_issue_1017_sm_unwind`` for
    the regression that pins this.

    Money conservation: ``net_delivered = gross_sold - tax - heloc_repaid``.
    If selling the WHOLE sleeve still cannot deliver ``net_need`` (the sleeve is
    too small, or underwater with ``sm_heloc > sm_fmv``), the whole sleeve is
    sold and ``net_delivered`` is whatever remains (possibly 0) -- a loud,
    honest shortfall, not a fabricated fill (DP#32).

    Args:
        net_need: the after-tax NET dollars the decumulation still needs after
            the ordinary financial drawdown exhausted every drawable account.
        sm_fmv: SM portfolio fair market value (the ``sm_investment_balance``
            sleeve).
        sm_acb: SM portfolio adjusted cost base (``sm_investment_cost_basis``).
        sm_heloc: the SM readvance line debt financing the sleeve.
        brackets: combined federal+provincial brackets (progressive pricing).
        other_income: taxable income the SM gain stacks on top of.
        inclusion_rate: capital-gains inclusion (default 0.5).
        flat_rate: deprecated flat fallback when no brackets (matches
            ``_price_source_draw``'s contract).

    Returns:
        SmUnwindResult. All zero when ``net_need <= 0`` or ``sm_fmv <= 0``
        (no shortfall, or no sleeve to unwind) -- inert (DP#32).
    """
    if net_need <= 0.0 or sm_fmv <= 0.0:
        return SmUnwindResult()
    gain_frac = ((sm_fmv - sm_acb) / sm_fmv) if sm_fmv > 0 else 0.0
    # Issue #110: `gain_frac` is SIGNED and NOT clamped to [0,1]. An underwater
    # pot (sm_fmv < sm_acb) realises a genuine capital LOSS; the pre-#110
    # `max(0.0, gain_frac)` floor silently swallowed it -- the ACB still
    # dropped proportionally while no loss was booked anywhere, so the
    # surviving sleeve carried acb > fmv with the loss hidden. Mirror
    # liquidation_waterfall.capital_gains_cost's signed-and-unclamped gain_frac
    # and price the loss against this year's other taxable income below.
    # Proportional HELOC repayment: selling fraction f of the sleeve retires
    # f * sm_heloc of the loan. Capped at the proceeds (you cannot repay more
    # debt than the sale raised -- the underwater case sm_heloc > sm_fmv).
    heloc_frac = (sm_heloc / sm_fmv) if sm_fmv > 0 else 0.0

    def _net_of_gross(gross: float):
        """Returns (net_delivered, tax, heloc_repaid) for selling ``gross``
        of the sleeve. `taxable_gain` is SIGNED (issue #110): positive for a
        gain (stacks on other_income as added tax), negative for a capital
        loss (its deductible slice offsets the year's other_income)."""
        taxable_gain = gross * gain_frac * inclusion_rate
        if brackets:
            from tax_calculator import tax_on_income
            tax = (tax_on_income(other_income + taxable_gain, brackets)
                    - tax_on_income(other_income, brackets))
        else:
            tax = taxable_gain * max(0.0, min(0.95, flat_rate))
        heloc_repaid = min(gross * heloc_frac, max(0.0, gross - tax))
        return gross - tax - heloc_repaid, tax, heloc_repaid

    full_net, full_tax, full_heloc = _net_of_gross(sm_fmv)
    if full_net <= net_need or full_net <= 0.0:
        # The whole sleeve cannot deliver the need (or delivers nothing):
        # sell it all, take the honest residual. realized_gain at full sale.
        gross = sm_fmv
        net, tax, heloc_repaid = full_net, full_tax, full_heloc
    else:
        # Binary-search the gross that delivers exactly net_need. net(gross)
        # is monotonic increasing within each linear tax band (each extra
        # dollar of gross adds to net its full value less the marginal
        # gain/loss tax-delta and the heloc repayment), so the search stays
        # well-posed even for an underwater loss, where the signed gain makes
        # the marginal "tax" a negative credit against other_income.
        lo, hi = 0.0, sm_fmv
        for _ in range(64):
            mid = 0.5 * (lo + hi)
            n, _, _ = _net_of_gross(mid)
            if n < net_need:
                lo = mid
            else:
                hi = mid
        gross = 0.5 * (lo + hi)
        net, tax, heloc_repaid = _net_of_gross(gross)

    realized_gain = gross * gain_frac
    return SmUnwindResult(
        # `tax` is SIGNED for issue #110: a loss's deductible slice offsets the
        # year's OTHER taxable income as a genuine credit, instead of being
        # floored to 0 -- priced through the bracket sweep above and capped by
        # the tax other_income actually owes (tax_on_income has a zero floor,
        # so an underwater pot booked against other income cannot push the
        # year's tax negative).
        gross_sold=gross,
        tax=tax,
        heloc_repaid=heloc_repaid,
        net_delivered=max(0.0, net),
        realized_gain=realized_gain,
    )
