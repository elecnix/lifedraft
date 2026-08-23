"""The ``amt`` rule: the year-end Alternative Minimum Tax assessment.

ITA s.127.5-127.6 (#710/#747/#754), plus Quebec's separate IMR. Runs DEAD LAST
in ``RULE_ORDER`` -- it is an assessment over ALL of the year's realized income,
so it must follow ``solvency``, whose forced liquidations can each realize a
capital gain the AMT base reads.

One module per government program (DP#10).

Split out of ``simulation_rules.py``; the rule body is unchanged.
"""

from __future__ import annotations

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule


@rule('amt')
def apply_amt(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Year-end Alternative Minimum Tax assessment (issue #710; ITA s.127.5-127.6).

    AMT ensures a taxpayer with large preference items pays at least a floor
    tax: the household is charged ``max(regular tax, minimum amount)``, and this
    rule books the SURCHARGE (minimum - regular) on top of the regular tax the
    fold already priced. It runs LAST in ``RULE_ORDER`` because AMT is a year-end
    assessment over ALL of the year's realized income -- after the retirement
    drawdown and every forced solvency liquidation have crystallized their gains.

    This is the wiring #710 asked for and #754 unblocked. ``countries.canada.amt``
    -- fully implemented, unit-tested, and called by NOTHING in production -- is
    invoked here on the year's realized income via ``total_tax_with_amt`` ->
    ``compute_amt`` (the ``max(regular, AMT)`` is handled inside). The regular
    federal tax the minimum is measured against is assembled from tax_calc's
    federal helpers (the same federal side as ``compute_total_tax``); the
    provincial tax and Quebec refundable credits play no part in the federal AMT
    comparison, so they are deliberately not pulled in here (their own wiring is
    #745).

    WHY THE REALIZED-CAPITAL-GAIN GATE IS THE WHOLE STORY (#754). With a correct
    s.127.52(1) base, the ONLY add-back big enough to lift AMTI above regular
    taxable income far enough to clear the basic exemption ABOVE regular tax is
    the 100% capital-gains inclusion (s.127.52(1)(d)): carrying charges are only
    half added back, the RRSP deduction is not an add-back at all, and the fold
    surfaces no stock-option benefit or loss carryover. So in a year the fold
    realizes NO capital gain, AMTI equals regular taxable income and the minimum
    amount can never exceed regular tax -- the surcharge is identically 0. The
    early return on ``realized_gain <= 0`` is that arithmetic, not a silent
    "assume no AMT" default (DP#32): it is the exact set of years in which AMT
    provably cannot bite, and it makes the assessment a strict no-op for every
    household the fold surfaces no realized gain for (e.g. the #581 golden
    household, which realizes 0 gains in all 46 years -> the golden invariant is
    unmoved by construction).

    MODELLED as of #747 (the three #710 deferrals, all cross-year/parallel):
      * the 50%-of-non-refundable-credits reduction to the minimum amount
        (ITA s.127.531) -- the same federal credits the regular tax is net of
        are passed to ``total_tax_with_amt``;
      * the 7-year AMT carry-forward (ITA s.120.2) -- AMT paid becomes a credit
        recovered against regular tax in a later year where regular tax exceeds
        the minimum amount, expiring after 7 years. The credit balance is
        cross-year state carried in ``ctx.amt_credit_opening`` and written out on
        ``ws.amt_credit_closing`` (pure fold, DP#26);
      * Quebec's separate impôt minimum de remplacement (19%, own exemption;
        TP-776.42) -- a SEPARATE surcharge booked on ``ws.qc_imr_surcharge``,
        with its own carry-forward, when the household is a Quebec resident.

    Reads: ``drawdown_realized_capital_gain`` + ``solvency_realized_gain`` (the
    year's 100%-inclusion realized gain, #754), ``drawdown_taxable`` /
    ``lif_withdrawal`` / government retirement income (already on ws), this
    year's grown employment income off ``ctx``, and the opening minimum-tax
    credit balances (``ctx.amt_credit_opening`` / ``ctx.qc_imr_credit_opening``).
    Writes: ``amt_surcharge`` / ``qc_imr_surcharge`` / ``amt_taxable_income``,
    the recovered-credit and closing-balance fields, and charges the NET tax
    (new surcharges minus recovered credits) against the non-registered pot.

    Returns True whenever a minimum tax is assessed OR a carried credit is
    recovered, so the #584 coverage sweep sees it fire and stay a no-op for a
    household that neither realizes a gain nor carries a credit (the golden
    household: opening balance empty and 0 realized gain every year, so the
    fast no-op below fires unconditionally and the invariant is unmoved).
    """
    realized_gain = (ws.drawdown_realized_capital_gain
                    + ws.solvency_realized_gain
                    + ws.heloc_servicing_realized_gain)
    # Fast no-op: with no realized gain AND no minimum-tax credit carried in,
    # neither a new minimum-tax assessment nor a recovery is possible (a year
    # with no gain has AMTI == regular taxable income, so the minimum cannot
    # exceed regular tax; with no opening credit there is nothing to recover).
    # The golden household hits this every one of its 46 years (DP#32).
    if realized_gain <= 0 and not ctx.amt_credit_opening and not ctx.qc_imr_credit_opening:
        return False

    # AMT parameters are year-versioned and only defined for a real tax year;
    # a direct unit-test caller that omits calendar_year gets the projection
    # INDEX here (0, 1, ...), which is not a tax year. The live run always
    # supplies the absolute calendar year (start_year + year), so this only
    # skips isolated rule-mechanics tests, never a real assessment.
    if ctx.calendar_year < 2024:
        return False

    from countries.canada.amt import (
        AMTParameters, total_tax_with_amt, carry_forward_amt_credit,
        QuebecIMRParameters, compute_quebec_imr,
    )
    from countries.canada.tax_calc import (
        compute_non_refundable_credits,
        federal_tax_before_abatement,
        quebec_abatement_amount,
        quebec_tax,
    )

    province = ctx.config.province
    year = ctx.calendar_year
    provider = default_tax_provider()

    # This year's ordinary taxable income: actual employment income (grown, and
    # pre-net of deductions -- a conservative overstatement that RAISES regular
    # tax and so can only SHRINK the surcharge, never fabricate one) plus every
    # taxable retirement component. A RETIRED member earns no salary this year,
    # so their (still-populated) pre-retirement grown income is excluded -- else
    # a retiree realizing a large gain would have regular tax computed as if the
    # salary were still coming in, wrongly suppressing the very AMT the gain owes.
    # The taxable (50%-included) slice of the realized gain already lives inside
    # drawdown_taxable (#754), so it is NOT re-added.
    employment_income = 0.0
    if not ctx.primary_retired:
        employment_income += ctx.primary_income_pre
    if not ctx.spouse_retired:
        employment_income += ctx.spouse_income_pre
    taxable_income = (
        employment_income
        + ws.drawdown_taxable
        + ws.heloc_servicing_taxable
        + ws.lif_withdrawal
        + ws.cpp_income
        + ws.oas_income
        + ws.pension_income
    )
    # The regular-inclusion (50%) slice of the realized gain -- already in
    # taxable_income -- that total_tax_with_amt grosses up to the AMT's 100%
    # inclusion (s.127.52(1)(d)).
    taxable_capital_gains = 0.5 * realized_gain

    # Regular FEDERAL tax after the Quebec abatement and federal non-refundable
    # credits -- the figure the minimum amount is measured against (CRA T691).
    # This is exactly the federal side of compute_total_tax; the AMT comparison
    # is federal-only, so the provincial tax and Quebec refundable credits
    # (solidarity/QPIP/FSS -- their own deliberate non-wiring, #745) are not part
    # of it and are not computed here.
    gross_fed = federal_tax_before_abatement(taxable_income, year, province, provider)
    abatement = quebec_abatement_amount(taxable_income, year, province, provider)
    nr_credits = compute_non_refundable_credits(
        employment_income, taxable_income, year, province, provider,
    )['total']
    federal_after_credits = max(0.0, gross_fed - abatement - nr_credits)

    # Pass the same federal non-refundable credits the regular tax is net of:
    # 50% of them reduce the minimum amount (ITA s.127.531, #747), so both sides
    # of the max(regular, minimum) comparison carry the credits.
    tax = total_tax_with_amt(
        regular_tax=federal_after_credits,
        taxable_income=taxable_income,
        taxable_capital_gains=taxable_capital_gains,
        capital_gains_inclusion=0.5,
        nonrefundable_credits=nr_credits,
        params=AMTParameters.for_year(year, provider),
    )
    surcharge = tax['amt_surcharge']
    ws.amt_taxable_income = taxable_income
    ws.amt_surcharge = surcharge

    # ── Federal 7-year carry-forward (ITA s.120.2, #747) ──
    # Recover a carried credit in a year regular tax exceeds the minimum amount,
    # up to that excess; book this year's own surcharge as a fresh 7-year credit.
    fed_room = max(0.0, federal_after_credits - tax['minimum_amount'])
    fed_recovered, fed_closing = carry_forward_amt_credit(
        ctx.amt_credit_opening, year, surcharge, fed_room,
    )
    ws.amt_credit_recovered = fed_recovered
    ws.amt_credit_closing = tuple(fed_closing)

    # ── Quebec impôt minimum de remplacement (TP-776.42, #747) ──
    # A separate provincial minimum tax (19%, own exemption), measured against
    # regular Quebec provincial tax, with its own 7-year carry-forward. Booked
    # on ws.qc_imr_surcharge, kept apart from the federal amt_surcharge because
    # they are two distinct taxes. QC non-refundable credits are not recomputed
    # here (the fold prices them elsewhere), so the QC minimum is not reduced by
    # them -- a conservative overstatement of the QC layer, never a fabrication.
    qc_surcharge = 0.0
    qc_recovered = 0.0
    qc_closing = tuple(ctx.qc_imr_credit_opening)
    if province in ('quebec', 'qc'):
        regular_qc = quebec_tax(taxable_income, year, provider)
        imr = compute_quebec_imr(
            regular_qc_tax=regular_qc,
            adjusted_income=tax['adjusted_income'],
            params=QuebecIMRParameters.for_year(year, provider),
        )
        qc_surcharge = imr['imr_surcharge']
        qc_room = max(0.0, regular_qc - imr['imr_minimum'])
        qc_recovered, qc_closing_list = carry_forward_amt_credit(
            ctx.qc_imr_credit_opening, year, qc_surcharge, qc_room,
        )
        qc_closing = tuple(qc_closing_list)
    ws.qc_imr_surcharge = qc_surcharge
    ws.qc_imr_credit_recovered = qc_recovered
    ws.qc_imr_credit_closing = qc_closing

    # ── Net cash effect on the non-registered pot ──
    # New minimum-tax surcharges (federal + QC) are charged to the non-reg pot
    # whose disposition triggered them; recovered credits are a genuine tax
    # refund credited BACK to it. Net them so a year that only recovers (a later
    # low-minimum year) restores cash, and a year that only pays draws it.
    new_charge = surcharge + qc_surcharge
    recovered = fed_recovered + qc_recovered
    net_charge = new_charge - recovered
    if net_charge > 0:
        # ACB is floored to the reduced balance so acb <= fmv holds (paying the
        # tax is a cash draw at book value, not a further taxable disposition).
        funded = min(net_charge, ws.new_nonreg_bal)
        ws.new_nonreg_bal -= funded
        if ws.new_nonreg_acb > ws.new_nonreg_bal:
            ws.new_nonreg_acb = ws.new_nonreg_bal
    elif net_charge < 0:
        # Net refund: the recovered credit reduced regular tax below what was
        # charged. Reinvest it in the non-reg pot at book value (bal and acb
        # rise together, so acb <= fmv is preserved).
        refund = -net_charge
        ws.new_nonreg_bal += refund
        ws.new_nonreg_acb += refund

    return surcharge > 0 or qc_surcharge > 0 or recovered > 0
