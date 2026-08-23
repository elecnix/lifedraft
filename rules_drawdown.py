"""Decumulation rules: the discretionary draw, and the one the statute forces.

``retirement_drawdown`` sizes and sources the NET spending target across the
declared drawdown order, re-bracketing per spouse and pricing the OAS clawback;
``rrif_minimum`` charges the mandatory RRIF minimum at 71 (#574) through the
SAME re-bracketing machinery (#825) and nets its after-tax proceeds into the
discretionary sizing (#1001).

Both price against POST-growth balances, so both run after every growth rule;
their relative order (drawdown, then forced minimum) is unchanged.

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule


@rule('retirement_drawdown')
def apply_retirement_drawdown(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Draw from registered/non-reg accounts (per ``drawdown_order``) to
    cover the NET spending need not met by CPP/OAS/pension
    (issues #294/#363/#579). Depends on every growth/contribution rule
    above -- it prices against POST-growth balances.
    """
    # Issue #1033: route the s.20(1)(c) investment-interest deduction THROUGH
    # the OAS-clawback base and the draw's progressive base. Placed BEFORE the
    # ``drawdown_net_target <= 0`` early return below (Blocker 2: a retiree
    # whose spending is covered without a discretionary draw still has the
    # forced RRIF minimum booking recovery tax in ``apply_rrif_minimum``, which
    # reads ``drawdown_other_taxable_income_primary`` -- so the base reduction
    # must fire even when no discretionary draw is taken). Gated on
    # ``ctx.primary_retired`` (the deduction is the PRIMARY's; Canada has no
    # joint filing) so the primary's deduction never subtracts from the
    # spouse's base in a mixed-phase year (primary working, spouse retired).
    #
    # Major 3: ``sm_interest_deduction`` is the FEDERAL ``total_deductible``
    # (uncapped), not the Quebec-capped ``qc_deductible`` -- the OAS recovery
    # tax is federal and the federal s.20(1)(c) deduction has no investment-
    # income limit. Major 4: the base is FLOORED at 0 -- a deduction larger
    # than the base is a non-capital loss / carry-forward (not modeled here,
    # follow-up), not a silently-forfeited negative base charged at the bottom
    # bracket. Moderate 5: the bracket-fill ceiling sits on the OAS-inclusive
    # base (other_taxable + oas), so it is recomputed consistently from the
    # reduced base + oas (and the ceiling re-derived via ``bracket_ceiling``),
    # so an ``rrsp_bracket_fill`` household prices its draw on the lowered base
    # AND computes headroom on the lowered base -- no D-inconsistency.
    #
    # Exactly one mechanism fires per phase: in retirement the side-credit in
    # ``apply_sm_interest`` is zeroed (its gate), so the routing is the sole
    # capture (clawback relief + draw re-bracketing, both on the balance sheet
    # via ``ws.oas_income`` / retained assets); in accumulation the routing is
    # a no-op (primary not retired -> gate off) and the side-credit handles it.
    # Issue #1083 (the #1033 over-correction): the routing now reaches the
    # PRIMARY's OTHER retirement taxable income too. #1033's known limit (c) --
    # the deduction never offset the rental/loan slice the prologue's
    # ``_income_tax_by_adult`` taxes (employment is zeroed at retirement;
    # rental operating + private-loan interest income survive it), and where
    # the base floored at 0 the whole deduction was stranded -- is fixed here:
    # the share of the deduction the cpp+pension base cannot absorb (the
    # remainder after the floor) is routed against ``ctx.primary_taxable_
    # income`` and its statutory saving booked via
    # ``ws.sm_interest_nondrawdown_tax_saving`` -> ``apply_solvency`` (the
    # tuition_credit precedent: real cash, not an objective side-credit -- the
    # flat side-credit stays gated OFF in retirement, so #1033's double-count
    # does not return). The split is disjoint by construction (absorbed +
    # remainder == D), so no dollar of the deduction is captured twice.
    # Known limits, NOT fixed here: (a) the PRELIMINARY OAS clawback
    # ``member_retirement_income`` books in ``retirement_income`` (BEFORE
    # ``sm_interest`` in RULE_ORDER) on the un-reduced CPP+pension base, so it
    # does not see this deduction; (b) ``drawdown_net_target`` was sized in
    # ``retirement_income`` against the un-reduced base, so the deduction's
    # cash benefit (lower tax -> smaller shortfall) is not fed back into the
    # shortfall; (c) the QC-CAPPED slice (``qc_deductible``) and its
    # carry-forward are still worth $0 once the primary retires -- the
    # provincial cap's release is #1035, out of scope here; (d) the leveraged
    # portfolio's DISTRIBUTED income still never ENTERS the base (the deferred
    # income-flowing half).
    if ctx.primary_retired and ws.sm_interest_deduction > 0.0:
        _D = ws.sm_interest_deduction
        from tax_calculator import bracket_ceiling
        # Issue #1083: how much of D the PRIMARY's cpp+pension base can absorb
        # (read BEFORE the floor below). The excess is the retiree's
        # rental/loan slice's share -- routed further down.
        _base_primary_pre = ws.drawdown_other_taxable_income_primary
        _absorbed = min(_D, _base_primary_pre)
        _remainder = _D - _absorbed
        ws.drawdown_other_taxable_income = max(
            0.0, ws.drawdown_other_taxable_income - _D)
        ws.drawdown_other_taxable_income_primary = max(
            0.0, ws.drawdown_other_taxable_income_primary - _D)
        # Recompute the OAS-inclusive bracket-fill base consistently from the
        # reduced base (the headroom is bracket_target - bracket_fill_base, so a
        # lower base grows the room before the ceiling -- the deduction frees
        # bracket-fill room, correctly). The base is a float (never None) where
        # the routing fires (retirement_income, which runs first, sets it).
        ws.drawdown_bracket_fill_base = (
            ws.drawdown_other_taxable_income + ws.drawdown_oas_gross)
        ws.drawdown_bracket_fill_base_primary = (
            ws.drawdown_other_taxable_income_primary
            + ws.drawdown_oas_gross_primary)
        # NEW-1: re-derive the ceiling ONLY when it was auto-derived. An
        # EXPLICIT retirement.bracket_fill_target (DP#13) is a fixed dollar
        # ceiling the household declared -- overwriting it with a re-derived
        # bracket_ceiling silently discards the election (the reviewer captured
        # a declared $40,000 overridden to $54,345, a 3.8x over-draw). The base
        # still drops (headroom = explicit - reduced_base grows by D), which is
        # the deduction's correct effect on a fixed ceiling.
        if ctx.year_brackets is not None and not ws.drawdown_bracket_target_explicit:
            ws.drawdown_bracket_target = bracket_ceiling(
                ws.drawdown_bracket_fill_base, ctx.year_brackets)
            ws.drawdown_bracket_target_primary = bracket_ceiling(
                ws.drawdown_bracket_fill_base_primary, ctx.year_brackets)
        # Issue #1083: route the REMAINDER against the primary's prologue-taxed
        # slice. ``ctx.primary_taxable_income`` is exactly the base the
        # prologue's ``_income_tax_by_adult`` taxed (employment zeroed at
        # retirement; rental operating + private-loan interest income survive),
        # and its tax already sits inside ``ctx.after_tax_income`` -- so the
        # statutory saving is booked as cash by ``apply_solvency``, the same
        # path the tuition_credit rule's reduction takes (one booking spelling,
        # DP#9). Valued at bracket-fill via ``deduction_value`` -- the SAME
        # ``tax_on_income`` / year-brackets path that taxed the slice, so a
        # deduction crossing a bracket boundary is worth its actual marginal
        # dollars, never a flat top rate (the pre-#1033 mechanism that is NOT
        # returning here: the objective's side-credit fields stay gated off).
        # Disjoint from the drawdown-base capture by construction
        # (``_absorbed + _remainder == _D``): no dollar offsets two incomes.
        # A remainder beyond this slice is a non-capital loss / carry-forward
        # (not modeled -- same follow-up family as Major 4's floor).
        if _remainder > 0.0 and ctx.primary_taxable_income > 0.0:
            from tax_calculator import deduction_value
            if ctx.year_brackets is not None:
                ws.sm_interest_nondrawdown_tax_saving = deduction_value(
                    ctx.primary_taxable_income, _remainder, ctx.year_brackets)
            else:
                # Direct unit-test callers without brackets keep the flat-rate
                # valuation, byte-for-byte the side-credit's own fallback
                # pattern in ``apply_sm_interest`` (the live fold always
                # passes ``year_brackets``).
                ws.sm_interest_nondrawdown_tax_saving = (
                    _remainder * ctx.primary_marginal_rate)
    if ws.drawdown_net_target <= 0:
        return False

    from countries.canada.retirement_transition import plan_drawdown_net

    draw_canada = {
        'tfsa_primary_balance': ws.new_tfsa_p_bal,
        'tfsa_spouse_balance': ws.new_tfsa_sp_bal,
        'rrsp_balance': ws.new_rrsp_bal,
        'spousal_rrsp_balance': ws.new_spousal_rrsp_bal,
        'spouse_rrsp_balance': ws.new_spouse_rrsp_bal,
        'lif_balance': ws.new_lif_balance,
        'lira_balance': ws.new_lira_balance,
        'fhsa_balance': ws.new_fhsa_bal,
    }
    order = ws.drawdown_order or ['tfsa', 'non_reg', 'rrsp']
    # Issue #1009: liquidate-to-target residual sweep. When the household opts
    # into the die-with-(near)-zero mode, append every drawable FINANCIAL token
    # the configured ``order`` did not already name, so the drawdown liquidates
    # ALL drawable savings (across the accounts the two-slot WorkingState
    # prices: both spouses' TFSA/RRSP, the household non-reg, the slot-0
    # FHSA/LIF) to meet the net target before a shortfall is reported -- rather
    # than delivering $0 while a LIF/FHSA the configured order never named sits
    # and compounds. The configured priority is preserved (the tail runs AFTER
    # the user's tokens); only MISSING tokens are appended.
    #
    # 'lira' is deliberately EXCLUDED from the tail: a LIRA is statutorily
    # locked until it converts to a LIF (age 71 / elected earlier), so drawing
    # it pre-conversion would be a statutory over-draw. Post-conversion its
    # balance is 0 anyway, and the LIF (the decumulation vehicle) IS in the tail.
    # If the configured order uses the capped 'rrsp_bracket_fill' token, the
    # uncapped 'rrsp' token is appended after it -- the bracket-fill priority
    # is preserved (the RRSP fills the chosen bracket first), and liquidate-
    # to-target then draws the RRSP remainder, which is exactly what the
    # die-with-zero mode asked for (the two tokens map to the same balance
    # keys, and the second draw sees the balance already reduced by the first,
    # so there is no double-count).
    # The principal residence is NOT a drawable financial account and is absent
    # from the tail by construction (it is not a _DRAWDOWN_SOURCES token); the
    # spouse's slot-1 LIRA/LIF/FHSA are not yet read into this two-slot
    # WorkingState (#901 follow-up).
    #
    # LIF statutory MAXIMUM (#1002): the residual tail appends 'lif' to the
    # order, so #1002's ``lif_discretionary_ceiling`` block below -- which caps
    # the 'lif' token at ``max(0, ws.lif_maximum_withdrawal - ws.lif_withdrawal)``
    # whenever 'lif' is in the order -- AUTOMATICALLY caps the residual LIF draw
    # too. There is ONE LIF-ceiling mechanism (#1002's), reused by the residual
    # sweep by construction; no second spelling (DP#9), no double-cap. Quebec
    # 55+ has no maximum (lif_maximum_withdrawal returns the full balance), so
    # the residual sweep can liquidate the whole LIF; federal respects the
    # factor ceiling and the LIF drains over years instead of in one.
    if ws.liquidate_to_target:
        _RESIDUAL_TAIL = ('tfsa', 'non_reg', 'rrsp', 'fhsa', 'lif')
        _configured = set(order)
        _tail = [t for t in _RESIDUAL_TAIL if t not in _configured]
        if _tail:
            order = list(order) + _tail
    # Issue #363 PR 4: when both spouses are retired, split the taxable draw
    # across their two SEPARATE bracket sets (each spouse's RRSP/RRIF priced
    # against — and clawing back OAS from — that spouse's own income). The net
    # target stays the single pooled `drawdown_net_target` (money conservation
    # unchanged). In a one-retiree year `per_member` stays None and the household
    # schedule prices the whole draw exactly as pre-PR-4.
    per_member = None
    if ws.drawdown_two_member_split:
        per_member = {
            'primary': {
                'other_taxable_income': ws.drawdown_other_taxable_income_primary,
                'oas_gross': ws.drawdown_oas_gross_primary,
                'bracket_target': ws.drawdown_bracket_target_primary,
                'bracket_fill_base': ws.drawdown_bracket_fill_base_primary,
            },
            'spouse': {
                'other_taxable_income': ws.drawdown_other_taxable_income_spouse,
                'oas_gross': ws.drawdown_oas_gross_spouse,
                'bracket_target': ws.drawdown_bracket_target_spouse,
                'bracket_fill_base': ws.drawdown_bracket_fill_base_spouse,
            },
        }
    # Issue #1002: the LIF statutory maximum caps the DISCRETIONARY LIF draw
    # so the forced minimum (apply_lira_lif, already taken and recorded on
    # ws.lif_withdrawal) + this discretionary draw never exceed the annual
    # statutory ceiling (ws.lif_maximum_withdrawal, computed on the opening/
    # converted LIF balance by apply_lira_lif). The ceiling is the RESIDUAL
    # room after the forced slice; ``lif_maximum_withdrawal`` is 0.0 in a year
    # with no LIF activity, so the cap binds at 0 and any 'lif' draw falls
    # through to the next source -- a hard zero, not a fallback (DP#32). Only
    # passed when the drawdown order actually contains the 'lif' token, so a
    # household with no LIF in its order (the default/golden path) never opts
    # into the cap (None disables, DP#13) and the path is byte-identical to
    # pre-#1002.
    lif_discretionary_ceiling = None
    if 'lif' in order:
        lif_discretionary_ceiling = max(
            0.0, ws.lif_maximum_withdrawal - ws.lif_withdrawal)
    plan = plan_drawdown_net(
        ws.drawdown_net_target, order, draw_canada, ws.new_nonreg_bal,
        ws.new_nonreg_acb, ws.retiree_marginal_rate,
        bracket_target=ws.drawdown_bracket_target,
        other_taxable_income=ws.drawdown_other_taxable_income,
        brackets=ctx.year_brackets,
        bracket_fill_base=ws.drawdown_bracket_fill_base,
        oas_gross=ws.drawdown_oas_gross,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        per_member=per_member,
        lif_max_withdrawal=lif_discretionary_ceiling)

    ws.drawdown_total = plan.total_withdrawn
    ws.drawdown_taxable = plan.taxable_withdrawn
    ws.drawdown_net_delivered = plan.net_delivered
    # Issue #754: surface the raw realized capital gain the non-reg disposition
    # crystallized (proceeds - ACB, 100%). The taxable slice of it is already in
    # plan.taxable_withdrawn at the cg_inclusion rate; this is the pre-inclusion
    # figure the year-end AMT base reads.
    ws.drawdown_realized_capital_gain = plan.realized_capital_gain

    # Issue #825: the per-spouse taxable draw recognized this year, so the
    # forced RRIF minimum (apply_rrif_minimum, later in the fold) can re-bracket
    # its own forced slice on TOP of it (per spouse) instead of on a flat
    # placeholder. In the per-member split each owner's taxable is explicit; in
    # the single 'household' schedule the whole taxable draw belongs to the sole
    # retiree, so attribute it to whichever member is retired.
    tbo = plan.taxable_by_owner or {}
    if 'household' in tbo:
        if ctx.spouse_retired and not ctx.primary_retired:
            ws.drawdown_taxable_spouse = tbo['household']
        else:
            ws.drawdown_taxable_primary = tbo['household']
    else:
        ws.drawdown_taxable_primary = tbo.get('primary', 0.0)
        ws.drawdown_taxable_spouse = tbo.get('spouse', 0.0)

    # Issue #363 PR 2: book the OAS clawback the taxable draw triggered. The
    # draw grossed up to REPLACE the clawed OAS (plan.net_delivered already
    # includes it), so reduce booked OAS income by the recovery tax and raise
    # the net target by the same amount. This is SINGLE-COUNT and cash-flow
    # neutral in the solvency identity (apply_solvency): available adds
    # drawdown_net_delivered (+clawback) and oas_income (-clawback), which
    # cancel — the real effect is the larger gross leaving the RRSP (money
    # conservation: the extra gross is drawn and remitted, and target ==
    # delivered so check_drawdown_meets_net_target still holds to the dollar).
    if plan.oas_clawback > 0:
        ws.oas_income -= plan.oas_clawback
        ws.drawdown_net_target += plan.oas_clawback

    for key, delta in plan.balance_deltas.items():
        if key == 'non_reg_balance':
            ws.new_nonreg_bal += delta
            if ws.new_nonreg_bal >= 0 and ws.new_nonreg_acb > 0:
                ws.new_nonreg_acb = max(0.0, ws.new_nonreg_acb + delta)
        elif key == 'tfsa_primary_balance':
            ws.new_tfsa_p_bal += delta
        elif key == 'tfsa_spouse_balance':
            ws.new_tfsa_sp_bal += delta
        elif key == 'rrsp_balance':
            ws.new_rrsp_bal += delta
            ws.spend_draw_primary_rrsp += -delta
        elif key == 'spousal_rrsp_balance':
            ws.new_spousal_rrsp_bal += delta
            ws.spend_draw_spouse_rrsp += -delta
        elif key == 'spouse_rrsp_balance':
            ws.new_spouse_rrsp_bal += delta
            ws.spend_draw_spouse_rrsp += -delta
        elif key == 'lif_balance':
            ws.new_lif_balance += delta
        elif key == 'lira_balance':
            ws.new_lira_balance += delta
        elif key == 'fhsa_balance':
            ws.new_fhsa_bal += delta

    # Issue #707: surface the decumulation shortfall -- AFTER the deltas are
    # applied, so `remaining` is the true post-drawdown balance. The drawdown
    # plan drew against every account in `order`; if it still delivered less
    # NET than the target, that is only "correct, accounts-empty behaviour"
    # (per check_drawdown_meets_net_target) when nothing was left to draw --
    # and in that case the gap MUST be recorded on the year, not silently
    # swallowed. A year where delivered < target but a meaningful balance
    # remained is a drawdown-sizing bug (the existing invariant flags it);
    # this field is the OTHER branch: accounts exhausted, gap unfunded.
    if ws.drawdown_net_target > 0:
        _gap = ws.drawdown_net_target - ws.drawdown_net_delivered
        if _gap > 1.0:
            _remaining = (
                ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
                + ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal
                + ws.new_nonreg_bal + ws.new_lif_balance + ws.new_lira_balance
                + ws.new_fhsa_bal
            )
            if _remaining <= 1.0:
                ws.drawdown_shortfall = _gap

    return plan.total_withdrawn > 0

@rule('rrif_minimum')
def apply_rrif_minimum(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Force out the mandatory RRIF minimum (CRA T4040 age factor x the
    OPENING Jan-1 balance) beyond whatever the spending drawdown already
    took. Depends on ``retirement_drawdown`` (spend_draw_*) and every
    growth rule (the forced excess is reinvested into the grown non-reg
    balance) -- runs last because it needs both.
    """
    forced_primary = 0.0
    forced_spouse = 0.0
    if ws.rrif_min_rate_primary > 0 and ws.new_rrsp_bal > 0:
        min_primary = ws.opening_rrsp_balance * ws.rrif_min_rate_primary
        forced = max(0.0, min_primary - ws.spend_draw_primary_rrsp)
        forced = min(forced, ws.new_rrsp_bal)
        ws.new_rrsp_bal -= forced
        forced_primary += forced
    if ws.rrif_min_rate_spouse > 0:
        min_spouse = (ws.opening_spouse_rrsp_balance + ws.opening_spousal_rrsp_balance) * ws.rrif_min_rate_spouse
        forced = max(0.0, min_spouse - ws.spend_draw_spouse_rrsp)
        take_spouse = min(forced, ws.new_spouse_rrsp_bal)
        ws.new_spouse_rrsp_bal -= take_spouse
        take_spousal = min(forced - take_spouse, ws.new_spousal_rrsp_bal)
        ws.new_spousal_rrsp_bal -= take_spousal
        forced_spouse += take_spouse + take_spousal

    forced_rrif_total = forced_primary + forced_spouse
    ws.forced_rrif_total = forced_rrif_total
    if forced_rrif_total <= 0:
        return False

    ws.drawdown_total += forced_rrif_total
    ws.drawdown_taxable += forced_rrif_total

    # Issue #825: price the tax on the forced RRIF minimum through the SAME
    # progressive re-bracketing (#363 PR 1) + OAS-clawback (#363 PR 2) machinery
    # the discretionary drawdown uses, per spouse (#363 PR 4) — not the old flat
    # placeholder rate that skipped the clawback. Each spouse's forced slice
    # re-brackets on top of that spouse's own already-recognized income (CPP /
    # pension + the discretionary taxable draw), and books the INCREMENTAL OAS
    # recovery tax it triggers as reduced OAS income (mirroring the discretionary
    # path's `ws.oas_income -= plan.oas_clawback`). The after-income-tax proceeds
    # are reinvested in non-reg; the clawback is a real reduction in OAS income.
    from countries.canada.retirement_transition import price_forced_rrif_tax
    tax_p, claw_p = price_forced_rrif_tax(
        other_taxable_income=ws.drawdown_other_taxable_income_primary,
        oas_gross=ws.drawdown_oas_gross_primary,
        prior_taxable_draw=ws.drawdown_taxable_primary,
        forced_taxable=forced_primary,
        brackets=ctx.year_brackets,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        flat_rate=ws.retiree_marginal_rate)
    tax_s, claw_s = price_forced_rrif_tax(
        other_taxable_income=ws.drawdown_other_taxable_income_spouse,
        oas_gross=ws.drawdown_oas_gross_spouse,
        prior_taxable_draw=ws.drawdown_taxable_spouse,
        forced_taxable=forced_spouse,
        brackets=ctx.year_brackets,
        oas_clawback_threshold=ws.drawdown_oas_threshold,
        flat_rate=ws.retiree_marginal_rate)

    income_tax = tax_p + tax_s
    oas_clawback = claw_p + claw_s
    after_tax_cash = max(0.0, forced_rrif_total - income_tax)
    # Issue #1001: the after-tax cash funds the net spending need FIRST (up to
    # the pre-RRIF shortfall priced in apply_retirement_income), and only the
    # EXCESS is reinvested into non-reg. The spending-funded slice flows to the
    # cash-flow identity via ws.rrif_after_tax_to_spending (added to solvency
    # `available`), replacing the discretionary TFSA draw that used to fund it.
    # Money is conserved: the RRIF gross leaves the RRSP, the income tax leaves
    # the household, the spending slice is consumed by spending, and the excess
    # reinvests into non-reg — the discretionary draw is smaller by the spending
    # slice, so TFSA stays higher (the wealth gain the fix produces).
    to_spending = min(after_tax_cash, ws.drawdown_net_target_pre_rrif)
    reinvest = after_tax_cash - to_spending
    ws.rrif_after_tax_to_spending = to_spending
    ws.new_nonreg_bal += reinvest
    ws.new_nonreg_acb += reinvest
    if oas_clawback > 0:
        ws.oas_income -= oas_clawback
    return True
