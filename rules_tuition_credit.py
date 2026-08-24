"""The ``tuition_credit`` rule: ITA s.118.5 / s.118.61 / s.118.8, and Quebec's.

Own-credit application with carry-forward (#784) plus inter-member and child
transfers (#785, capped at the credit on $5,000 of tuition). Runs immediately
before ``solvency`` -- its sole consumer, which counts the POST-credit
after-tax income as ``available`` in the cash-flow identity.

One module per government program (DP#10).

Split out of ``simulation_rules.py``; the rule body is unchanged.
"""

from __future__ import annotations

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule


# Epic #795 bite 3 (DP#9/#26): the pure carry-forward arithmetic the
# `tuition_credit` rule composes. This used to live as
# ``simulation._apply_tuition_credits_with_carryforward`` and was spelled twice
# in the fold's prologue; it is now a single module-private helper the
# registered rule calls (the orchestration moved to the rule, DP#9 -- one
# spelling, not two). Pure (DP#3): a function of the credit, the opening
# carry-forward, and the tax before the credit. Kept as a standalone helper so
# the carry-forward semantics remain directly unit-testable (#784's own tests
# import it).
def _apply_tuition_credit_with_carryforward(
        this_year: float, carryforward: float, tax_before: float
) -> tuple:
    """Apply a non-refundable tuition credit with carry-forward of the
    unused remainder (issue #784).

    ``credit_available = this_year_credit + carried_forward_from_prior_years``;
    ``applied = min(credit_available, tax_before_credit)`` (the credit is
    non-refundable -- it can never make tax negative, so `applied` never
    exceeds `tax_before`); ``new_carryforward = credit_available - applied``
    (the unused remainder carries to the next year -- CRA / Revenu Québec
    both allow indefinite carry-forward).
    """
    available = this_year + carryforward
    if available <= 0.0:
        return 0.0, max(0.0, carryforward)  # nothing to apply; carry any prior unused
    if tax_before <= 0.0:
        return 0.0, available  # no tax to reduce -> all carries forward
    applied = min(available, tax_before)
    return applied, available - applied


# Issue #785: the federal tuition-credit TRANSFER limit (ITA s.118.8) is on
# the TUITION AMOUNT, not the credit: a student may transfer up to $5,000 of
# eligible tuition fees to a supporting spouse/parent. In credit space, the
# cap is the credit on $5,000 of tuition at the federal lowest rate.
_FEDERAL_TUITION_TRANSFER_LIMIT = 5000.0  # dollars of tuition (ITA s.118.8)

@rule('tuition_credit')
def apply_tuition_credit(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Epic #795 bite 3 (DP#10/#26): the per-year federal (+ QC provincial)
    tuition tax credit.

    A taxed member studying in ``sim_year`` claims their OWN non-refundable
    credit on the eligible tuition they paid that year
    (``member['tuition_by_year'][sim_year]``); the credit is subtracted from
    that member's ``tax_on_income``, floored at 0 (non-refundable -- it
    cannot make tax negative). The unused remainder CARRIES FORWARD to reduce
    a future year's tax (#784, CRA / Revenu Québec indefinite carry-forward).
    A member (or a child, #701) with ``tuition_transfer_to`` declared
    transfers unused credit to a supporting spouse/parent, capped at the
    credit on $5,000 of tuition (ITA s.118.8, #785).

    This used to be computed inline in the fold's prologue -- spelled TWICE,
    once in ``simulate_year`` and once in ``_run_monthly`` (DP#9, two parallel
    implementations of the same wiring, split across three helpers:
    ``simulation._tuition_credits_for`` /
    ``_apply_tuition_credits_with_carryforward`` /
    ``_process_tuition_transfers``). It is now a single registered rule; the
    prologue passes only the primitive inputs the rule needs (each member's
    pre-credit ``tax_on_income`` + the resolved ``tax_provider`` + the province
    + the opening carry-forwards, the latter read off ``ws``), and this rule
    writes its outputs to ``YearWorkingState``: the ACTUAL per-member tax
    reduction (own credit applied + transfers received, after all non-
    refundable flooring) and the new per-member / per-child carry-forwards.
    ``apply_solvency`` (next in ``RULE_ORDER``) reads the applied amounts so
    the cash-flow identity counts the POST-credit after-tax income as
    ``available`` (the credit reduces tax, so it raises after-tax income);
    the epilogue and ``build_year_result`` surface the new carry-forwards.

    DP#25: ``countries.canada`` is imported lazily inside the body (this
    module keeps no jurisdiction import at top level). The body is the
    verbatim computation the three prologue helpers performed -- moved, not
    changed -- so every number is preserved byte-for-byte. Gated on declared
    tuition: absent ``tuition_by_year`` -> ``{}`` -> ``0.0`` credit, so a
    household that declares no tuition (incl. the golden fixture) is
    unaffected (a strict no-op -> the golden invariant is unchanged by
    construction).

    Returns True when any credit was applied or transferred this year (the
    rule had an observable effect), False for a year with no declared tuition
    and no carried-forward remainder (no-op, all outputs stay at their
    seeded 0.0 / [] defaults).
    """
    from countries.canada import tuition_tax_credit as _tuition_tax_credit

    config = ctx.config
    members = config.family_members
    primary_member = next((m for m in members if m.get('role') == 'primary'), {})
    spouse_member = next((m for m in members if m.get('role') == 'spouse'), {})

    sim_year = ctx.calendar_year
    tax_provider = ctx.tax_provider if ctx.tax_provider is not None else default_tax_provider()
    province = config.province

    # ── Per-member own credit (issue #764/#783) ──
    # Federal (+ QC provincial when province is QC) credit on the eligible
    # tuition each taxed member paid this year. 0.0 for a member with no
    # declared tuition that year (DP#32: absent tuition_by_year -> {} -> 0).
    primary_tuition = primary_member.get('tuition_by_year', {}).get(sim_year, 0.0)
    spouse_tuition = spouse_member.get('tuition_by_year', {}).get(sim_year, 0.0)
    primary_tuition_credit = _tuition_tax_credit(
        primary_tuition, sim_year, tax_provider,
        province=province) if primary_tuition else 0.0
    spouse_tuition_credit = _tuition_tax_credit(
        spouse_tuition, sim_year, tax_provider,
        province=province) if spouse_tuition else 0.0

    # ── Apply own credit with carry-forward (issue #784) ──
    primary_tax_before = ctx.primary_tax_before
    spouse_tax_before = ctx.spouse_tax_before
    primary_applied, primary_new_cf = _apply_tuition_credit_with_carryforward(
        primary_tuition_credit, ws.opening_primary_tuition_carryforward,
        primary_tax_before)
    spouse_applied, spouse_new_cf = _apply_tuition_credit_with_carryforward(
        spouse_tuition_credit, ws.opening_spouse_tuition_carryforward,
        spouse_tax_before)

    # ── Transfers to a supporting spouse/parent (issue #785, ITA s.118.8) ──
    # The transfer happens AFTER own-tax application and BEFORE carry-forward:
    # student applies to own tax, then transfers min(remaining, cap) to the
    # supporter, then carries forward the rest (#784). Two cases share the
    # SAME transfer mechanism: a TAXED member transfers their unused credit
    # (the new_cf above, i.e. what remained after applying to own tax); a
    # CHILD transfers their FULL credit (no own tax). Both capped at the
    # credit on $5,000 of tuition (federal only, province=None) and floored at
    # the supporter's REMAINING tax (tax after their own credit, before the
    # transfer) -- non-refundable, no phantom refund.
    transfer_cap = _tuition_tax_credit(
        _FEDERAL_TUITION_TRANSFER_LIMIT, sim_year, tax_provider, province=None)

    primary_id = primary_member.get('id', '')
    spouse_id = spouse_member.get('id', '')

    primary_remaining_tax = max(0.0, primary_tax_before - primary_applied)
    spouse_remaining_tax = max(0.0, spouse_tax_before - spouse_applied)

    def _supporter_tax(transfer_to: str) -> float:
        if transfer_to == primary_id:
            return primary_remaining_tax
        if transfer_to == spouse_id:
            return spouse_remaining_tax
        return 0.0

    to_primary = 0.0
    to_spouse = 0.0

    # Taxed-member transfers (secondary): transfer unused credit after own tax.
    if primary_member.get('tuition_transfer_to') and primary_new_cf > 0.0:
        transfer_to = primary_member['tuition_transfer_to']
        transferable = min(primary_new_cf, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        primary_new_cf -= transferred
        if transfer_to == primary_id:
            to_primary += transferred
        elif transfer_to == spouse_id:
            to_spouse += transferred
    if spouse_member.get('tuition_transfer_to') and spouse_new_cf > 0.0:
        transfer_to = spouse_member['tuition_transfer_to']
        transferable = min(spouse_new_cf, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        spouse_new_cf -= transferred
        if transfer_to == primary_id:
            to_primary += transferred
        elif transfer_to == spouse_id:
            to_spouse += transferred

    # Children transfers (the main case, #701): full credit transfers (no own tax).
    children = config.children
    child_cfs = list(ws.opening_child_tuition_carryforwards)
    while len(child_cfs) < len(children):
        child_cfs.append(0.0)
    new_child_cfs = list(child_cfs)

    for i, child in enumerate(children):
        child_tuition = child.get('tuition_by_year', {}).get(sim_year, 0.0)
        transfer_to = child.get('tuition_transfer_to')
        # Federal credit on the child's tuition (federal only for transfer).
        # 0 when no new tuition this year, but the carry-forward from prior
        # years is still available for transfer (#784).
        child_credit = _tuition_tax_credit(
            child_tuition, sim_year, tax_provider, province=None) if child_tuition else 0.0
        available = child_credit + child_cfs[i]
        if available <= 0.0:
            new_child_cfs[i] = 0.0
            continue
        if not transfer_to:
            new_child_cfs[i] = available
            continue
        transferable = min(available, transfer_cap)
        supporter_tax = _supporter_tax(transfer_to)
        transferred = min(transferable, max(0.0, supporter_tax))
        new_child_cfs[i] = available - transferred
        if transferred > 0.0:
            if transfer_to == primary_id:
                to_primary += transferred
            elif transfer_to == spouse_id:
                to_spouse += transferred

    # ── The ACTUAL per-member tax reduction this year ──
    # `primary_tax = max(0, primary_remaining_tax - to_primary)` in the old
    # prologue; the reduction is `tax_before - primary_tax`, i.e. the credit
    # dollars that actually lowered tax (after both non-refundable floors:
    # the own-credit floor and the transfer floor). apply_solvency adds this
    # to `available` so the identity sees the POST-credit after-tax income.
    primary_tax = max(0.0, primary_remaining_tax - to_primary)
    spouse_tax = max(0.0, spouse_remaining_tax - to_spouse)
    primary_reduction = primary_tax_before - primary_tax
    spouse_reduction = spouse_tax_before - spouse_tax

    ws.tuition_credit_applied_primary = primary_reduction
    ws.tuition_credit_applied_spouse = spouse_reduction
    ws.new_primary_tuition_carryforward = primary_new_cf
    ws.new_spouse_tuition_carryforward = spouse_new_cf
    ws.new_child_tuition_carryforwards = new_child_cfs

    return (
        primary_reduction > 0.0 or spouse_reduction > 0.0
        or primary_new_cf > 0.0 or spouse_new_cf > 0.0
        or any(cf > 0.0 for cf in new_child_cfs)
    )
