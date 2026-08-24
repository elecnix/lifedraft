"""Contribution-side rules: what goes INTO the registered plans, and the room.

``contributions`` (clamp + book the year's RRSP/TFSA/non-reg allocation),
``rrsp_ledger`` (the dated contribution ledger DP#19 tracks basis from),
``rrsp_deduction`` (when the deduction is actually claimed, incl. deduct-later
staggering), ``fhsa``, and ``contribution_room`` (next year's accrual).

Split out of ``simulation_rules.py``; the rule bodies are unchanged. They stay
together because they are the same side of the same accounts -- money in, and
the room that bounds it -- and they run adjacent in ``RULE_ORDER`` except for
``fhsa``/``contribution_room``, whose positions are unchanged.
"""

from __future__ import annotations

from typing import Dict

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule
from rules_growth import _blended_pot_rate


@rule('contributions')
def apply_contributions(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Clamp this year's allocations to available room and book them.

    First rule in the fold: every later rule (deduction, HELOC tracing,
    growth) reads the post-contribution balances/rooms this rule produces.
    """
    combined_rrsp_room = max(0, ws.opening_rrsp_room)
    p_rrsp_actual = min(ws.p_rrsp, combined_rrsp_room)
    remaining_rrsp_room = combined_rrsp_room - p_rrsp_actual
    s_rrsp_actual = min(ws.s_rrsp, remaining_rrsp_room)
    remaining_rrsp_room -= s_rrsp_actual
    sp_rrsp_actual = min(ws.sp_rrsp, max(0, ws.opening_spouse_rrsp_room))
    p_tfsa_actual = min(ws.p_tfsa, max(0, ws.opening_tfsa_primary_room))
    sp_tfsa_actual = min(ws.sp_tfsa, max(0, ws.opening_tfsa_spouse_room))

    # Issue #170 (DP#32): a declared contribution above room is a FACT, not an
    # absence -- record what was refused instead of letting the min() clamp
    # drop it silently. The booked amounts are unchanged (the clamp still
    # bounds what enters the plan); this only makes the refusal VISIBLE, on
    # the working state, every YearResult, and the model_fidelity caveat.
    ws.rrsp_refused_own = ws.p_rrsp - p_rrsp_actual
    ws.rrsp_refused_spousal = ws.s_rrsp - s_rrsp_actual

    ws.p_rrsp_actual = p_rrsp_actual
    ws.s_rrsp_actual = s_rrsp_actual
    ws.sp_rrsp_actual = sp_rrsp_actual
    ws.p_tfsa_actual = p_tfsa_actual
    ws.sp_tfsa_actual = sp_tfsa_actual

    ws.new_rrsp_bal = ws.opening_rrsp_balance + p_rrsp_actual
    ws.new_spousal_rrsp_bal = ws.opening_spousal_rrsp_balance + s_rrsp_actual
    ws.new_spouse_rrsp_bal = ws.opening_spouse_rrsp_balance + sp_rrsp_actual
    ws.new_tfsa_p_bal = ws.opening_tfsa_primary_balance + p_tfsa_actual
    ws.new_tfsa_sp_bal = ws.opening_tfsa_spouse_balance + sp_tfsa_actual
    ws.new_nonreg_bal = ws.opening_non_reg_balance + ws.non_reg_alloc
    ws.new_nonreg_acb = ws.opening_non_reg_acb + ws.non_reg_alloc

    ws.new_rrsp_room = max(0, remaining_rrsp_room)
    ws.new_spousal_rrsp_room = 0  # Spousal RRSP shares primary's room
    ws.new_spouse_rrsp_room = max(0, ws.opening_spouse_rrsp_room - sp_rrsp_actual)
    ws.new_tfsa_p_room = max(0, ws.opening_tfsa_primary_room - p_tfsa_actual)
    ws.new_tfsa_sp_room = max(0, ws.opening_tfsa_spouse_room - sp_tfsa_actual)

    return (p_rrsp_actual + s_rrsp_actual + sp_rrsp_actual
            + p_tfsa_actual + sp_tfsa_actual + ws.non_reg_alloc) > 0

@rule('rrsp_ledger')
def apply_rrsp_ledger(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Track this year's RRSP contributions in the per-contribution ledger
    (DP#19: deduction timing is recorded per contribution, not assumed).
    Depends on ``contributions`` for the *_actual amounts.
    """
    from simulation_state import RRSPListLedger

    # Issue #1059: the ledger entries are flat dicts of scalars (year, amount,
    #    role, deducted, deduction_year, deduction_marginal_rate) -- no nested
    #    containers.  Downstream mutates entries in place (claim_all_deductions
    #    sets e['deducted']=True; claim_deferred_deduction adjusts entry['amount']),
    #    so each dict MUST be a fresh copy to avoid corrupting the prior year's
    #    state (DP#26).  But deepcopy is overkill: a shallow dict copy per entry
    #    ({**e}) is enough because all values are immutable scalars.
    #    Verified: grep opening_rrsp_ledger.append/extend/[..]= -> empty;
    #    only .add_contribution (appends a new dict) and .claim_* (mutates
    #    existing entries' scalar values) touch the ledger after this point.
    new_ledger = RRSPListLedger([{**e} for e in ws.opening_rrsp_ledger])
    fired = False
    if ws.p_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.p_rrsp_actual, role='primary')
        fired = True
    if ws.s_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.s_rrsp_actual, role='spousal')
        fired = True
    if ws.sp_rrsp_actual > 0:
        new_ledger.add_contribution(year=ws.year, amount=ws.sp_rrsp_actual, role='spouse')
        fired = True
    ws.new_ledger = new_ledger
    return fired

@rule('rrsp_deduction')
def apply_rrsp_deduction(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Deduct-now or deduct-later (issue #546: bracket-fill staggering).
    Depends on ``rrsp_ledger`` (mutates the same ledger object) and
    ``contributions`` for the *_actual amounts.
    """
    rrsp_deduction_savings = 0.0
    spouse_deduction_savings = 0.0
    deduction_claims = []
    deduct_later_staggered_total = ws.opening_deduct_later_staggered_total
    deduct_later_first_claim_income = ws.opening_deduct_later_first_claim_income
    deduct_later_total_deducted = ws.opening_deduct_later_total_deducted

    if not ctx.deduct_later:
        ws.new_ledger.claim_all_deductions(year=ws.year, marginal_rate=ctx.primary_marginal_rate)
        rrsp_deduction_savings = (ws.p_rrsp_actual + ws.s_rrsp_actual) * ctx.primary_marginal_rate
        spouse_deduction_savings = ws.sp_rrsp_actual * ctx.spouse_marginal_rate
    else:
        # Issue #546: stagger the primary/spousal claim toward the bracket
        # target, valuing each year's slice at its bracket-fill marginal rate.
        if ws.sp_rrsp_actual > 0:
            spouse_deduction_savings = ws.sp_rrsp_actual * ctx.spouse_marginal_rate

        brackets = default_tax_provider().get_combined_brackets(ctx.config.start_year, province=ctx.config.province)
        primary_income_this_year = ctx.allocations.get('_primary_income', 0)
        claim = ws.new_ledger.claim_deferred_deduction(
            year=ws.year,
            income=primary_income_this_year,
            brackets=brackets,
            bracket_target=ctx.config.deduct_later_bracket_target,
        )
        rrsp_deduction_savings = claim['savings']
        deduction_claims = claim['claims']
        if claim['amount'] > 0:
            deduct_later_staggered_total += claim['savings']
            if deduct_later_total_deducted == 0:
                deduct_later_first_claim_income = primary_income_this_year
            deduct_later_total_deducted += claim['amount']

    # Issue #546: staggered bracket-fill total minus the bracket-fill value
    # of deducting the same total all in the first claim year.
    if deduct_later_total_deducted > 0:
        from tax_calculator import deduction_value
        adv_brackets = default_tax_provider().get_combined_brackets(ctx.config.start_year, province=ctx.config.province)
        lump_now_total = deduction_value(
            deduct_later_first_claim_income, deduct_later_total_deducted, adv_brackets)
        deduction_advantage_vs_now = deduct_later_staggered_total - lump_now_total
    else:
        deduction_advantage_vs_now = 0.0

    ws.rrsp_deduction_savings = rrsp_deduction_savings
    ws.spouse_deduction_savings = spouse_deduction_savings
    ws.deduction_claims = deduction_claims
    ws.deduct_later_staggered_total = deduct_later_staggered_total
    ws.deduct_later_first_claim_income = deduct_later_first_claim_income
    ws.deduct_later_total_deducted = deduct_later_total_deducted
    ws.deduction_advantage_vs_now = deduction_advantage_vs_now

    return (rrsp_deduction_savings + spouse_deduction_savings) > 0

@rule('fhsa')
def apply_fhsa(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """FHSA contribution (clamped to room and lifetime limit) + growth, and
    this year's annual room addition (issue #124, DP#20). Independent of
    every rule above.

    Issue #912: the FHSA grows at the same per-pot rate machinery the rrsp/tfsa
    pots use (``_blended_pot_rate``) so a declared foreign-equity composition
    drags its return via #641's ``registered_wht_drag`` -- an FHSA has no US-
    treaty exemption, so its foreign holdings leak like a TFSA's. Absent a
    declared fhsa composition (and override/fee) the blended rate IS the flat
    ``ctx.investment_return`` (golden no-op, DP#32).
    """
    from simulation_state import _canada_fhsa_limits

    fhsa_lifetime_remaining = max(0, ws.opening_fhsa_lifetime_limit - ws.opening_fhsa_lifetime_used)
    fhsa_actual = min(ctx.fhsa_contribution, max(0, ws.opening_fhsa_room), fhsa_lifetime_remaining)
    fhsa_pre_growth = ws.opening_fhsa_balance + fhsa_actual
    fhsa_rate = _blended_pot_rate(ctx, 'fhsa', fhsa_pre_growth)
    new_fhsa_bal = fhsa_pre_growth * (1 + fhsa_rate)
    new_fhsa_room = max(0, ws.opening_fhsa_room - fhsa_actual)
    new_fhsa_lifetime_used = ws.opening_fhsa_lifetime_used + fhsa_actual

    if ctx.fhsa_annual_limit is not None:
        capped_prior_room = min(new_fhsa_room, _canada_fhsa_limits()[1])
        new_fhsa_room = capped_prior_room + ctx.fhsa_annual_limit

    ws.fhsa_lifetime_remaining = fhsa_lifetime_remaining
    ws.fhsa_actual = fhsa_actual
    # Issue #893: what slot 0 could not absorb is the budget available to the
    # further owners' FHSAs (distributed in rebuild_adult_fhsa). ctx.fhsa_-
    # contribution is now sized against the TOTAL household FHSA room.
    ws.fhsa_overflow = max(0.0, ctx.fhsa_contribution - fhsa_actual)
    ws.new_fhsa_bal = new_fhsa_bal
    ws.new_fhsa_room = new_fhsa_room
    ws.new_fhsa_lifetime_used = new_fhsa_lifetime_used
    return fhsa_actual > 0

@rule('contribution_room')
def apply_contribution_room(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Add this year's annual RRSP/TFSA room (DP#20: year-specific limits
    from the tax data provider). Depends on ``contributions`` for the
    post-contribution room to add onto.

    Issue #674 (ITA s.146(1)): RRSP room accrues on EARNED income, not on
    every taxable dollar -- an Employment Insurance benefit is taxable but
    is NOT earned income, so it must add $0 room. ``simulation.py``'s
    ``_income_components_for_year`` computes ``_primary_earned_income``/
    ``_spouse_earned_income`` (kind-filtered) alongside the taxable
    ``_primary_income``/``_spouse_income`` and every production allocations-
    dict site sets both. The fallback to the taxable total below only fires
    when ``_primary_earned_income`` is truly ABSENT (a caller -- e.g. a unit
    test exercising this rule in isolation -- built an allocations dict that
    never tracked the earned/taxable split at all); an absent key means
    "unknown", not "zero" (DP#32), so it falls back to the pre-#674 taxable-
    income behaviour rather than silently crediting $0 room to a caller that
    was never asked about kinds.
    """
    rrsp_limit = ctx.rrsp_annual_limit if ctx.rrsp_annual_limit is not None else ctx.config.rrsp_annual_max
    tfsa_limit = ctx.tfsa_annual_limit if ctx.tfsa_annual_limit is not None else ctx.config.tfsa_annual_room_per_person
    primary_earned = ctx.allocations.get('_primary_earned_income')
    if primary_earned is None:
        primary_earned = ctx.allocations.get('_primary_income', 130000)
    spouse_earned = ctx.allocations.get('_spouse_earned_income')
    if spouse_earned is None:
        spouse_earned = ctx.allocations.get('_spouse_income', 50000)
    primary_room_added = min(rrsp_limit, ctx.config.rrsp_annual_percent * primary_earned)
    spouse_room_added = min(rrsp_limit, ctx.config.rrsp_annual_percent * spouse_earned)

    ws.new_rrsp_room += primary_room_added
    ws.new_spouse_rrsp_room += spouse_room_added
    ws.new_tfsa_p_room += tfsa_limit
    ws.new_tfsa_sp_room += tfsa_limit
    return (primary_room_added + spouse_room_added + tfsa_limit + tfsa_limit) > 0


def worst_rrsp_refusal(rows) -> Dict:
    """Reduce ranked-scenario ``rrsp_refusal`` summaries (the per-row dicts
    ``summarize_rrsp_refusal`` produces, carried on each ranking row) to the
    ONE summary the run-wide caveat names: the scenario that refused the MOST
    declared money; ties break to the earliest first-refused year. An empty or
    all-clear row set returns the all-clear summary (DP#32: "nothing was
    refused" is a checked result, not an absence).

    Pure function (DP#3); the optimize caller records its result onto
    ``assumptions.rrsp_contribution_refused`` for the fidelity caveat.
    """
    engaged = [r for r in rows
               if isinstance(r, dict) and r.get('engaged')]
    if not engaged:
        return {'engaged': False, 'first_refused_year': None,
                'refused_own_total': 0.0, 'refused_spousal_total': 0.0}
    return max(
        engaged,
        key=lambda row: (row.get('refused_own_total', 0.0)
                         + row.get('refused_spousal_total', 0.0),
                         -(row.get('first_refused_year')
                           if row.get('first_refused_year') is not None
                           else float('inf'))))


def summarize_rrsp_refusal(results) -> Dict:
    """Fold a trajectory's ``YearResult`` list into the RRSP-refusal facts a
    household needs (issue #170).

    Returns::

        {
          'engaged':              bool,  # any year refused a declared contribution
          'first_refused_year':   int | None,
          'refused_own_total':    float, # sum of own-RRSP refusals across the run
          'refused_spousal_total': float, # sum of spousal-RRSP refusals
        }

    Mirrors ``decumulation.summarize_drawdown_shortfall`` (#707): the facts are
    folded once from the trajectory and travel as DATA, so the model_fidelity
    caveat (which reads ``assumptions.rrsp_contribution_refused`` off the cfg)
    can surface them on every output surface without recomputing (DP#9/DP#3).
    """
    engaged = False
    first_year = None
    own_total = 0.0
    spousal_total = 0.0
    for r in results:
        own = getattr(r, 'rrsp_contribution_refused_own', 0.0)
        spousal = getattr(r, 'rrsp_contribution_refused_spousal', 0.0)
        if own > 0 or spousal > 0:
            engaged = True
            if first_year is None:
                first_year = getattr(r, 'year', None)
        own_total += own
        spousal_total += spousal
    return {
        'engaged': engaged,
        'first_refused_year': first_year,
        'refused_own_total': own_total,
        'refused_spousal_total': spousal_total,
    }
