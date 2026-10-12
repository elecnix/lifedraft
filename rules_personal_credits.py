"""The ``personal_credits`` rule: ITA s.118.1 / s.118.2 / s.127(3) and Quebec's
charitable-donation and medical-expense credits (issue #367).

A household could not declare the recurring outlays that generate personal tax
credits: medical expenses, charitable donations and federal political
contributions. The credit functions existed -- ``medical_expense_credit``,
``charitable_donation_credit`` and Quebec's two equivalents -- but nothing
reached them, so a senior paying for care, or a household that gives regularly,
was taxed as if it had claimed nothing and its after-tax cash was understated
every year.

This rule is the missing half of that path. It is registered BESIDE
``tuition_credit`` and immediately before ``solvency`` for the same reason the
tuition credit is: the prologue prices each adult's tax from the pre-credit
``tax_on_income``, and only a rule that writes a reduction for ``solvency`` to
consume can lower the tax the cash-flow identity sees. A credit computed on a
prologue path would be priced and then dropped.

One module per government program (DP#10) would put each of these credits in its
own home; they share one rule here because they share one MECHANISM -- a declared
per-member amount, a statutory floor or schedule, and a non-refundable cap --
and splitting them would mean three rules writing three reduction fields for
``solvency`` to add.

WHAT IS MODELLED, AND WHAT IS NOT

- Federal medical (ITA s.118.2), charitable (s.118.1) and political (s.127(3))
  credits, and Quebec's medical and charitable credits. The Quebec ones apply
  only to a Quebec resident (DP#10: the jurisdiction owns its own credits).
- The medical threshold uses the member's TAXABLE income as net income. That is
  the same reading `compute_non_refundable_credits` already takes (its
  ``net_income`` defaults to ``taxable_income``), and this engine has no
  separate net-income figure per member; it is stated here rather than left to
  be discovered.
- The federal political credit treats one declared amount as ONE recipient
  class. A household may give $1,275 to a party AND to a candidate AND to a
  leadership contestant, each earning its own credit; this engine would
  understate such a household. The schema leaf and
  ``political_contribution_credit`` both say so.

NOT modelled, and disclosed rather than silently absorbed: the institutional-care
split (a facility bill is a medical expense federally but only its LODGING
component is one in Quebec, the services share belonging to the home-support
credit) and the home-support credit itself are a later layer of #367.

DP#3: the rule reads ``ctx`` and the mapped config only. DP#32: a household that
declares no claims writes 0.0/0.0 and returns False -- a strict no-op, so the
golden invariant is unchanged by construction.
"""

from __future__ import annotations

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule


def _quebec_lowest_rate(provider, year: int, province: str) -> float:
    """Quebec's lowest bracket rate, the conversion rate for its non-refundable
    credits. Read from the PROVINCIAL slice of the split brackets so it is
    Quebec's own rate, not the blended federal-plus-Quebec lowest rate the
    combined list starts with (the two differ, and using the blended one would
    overvalue every Quebec credit). 0.0 when the split is unavailable, which
    makes the Quebec credits worth nothing rather than inventing a rate
    (DP#32)."""
    if province != "quebec":
        return 0.0
    try:
        _federal, provincial = provider.get_split_brackets(year, province=province)
    except ValueError:
        return 0.0
    if not provincial:
        return 0.0
    return float(provincial[0]["rate"])


@rule('personal_credits')
def apply_personal_credits(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #367: price each taxed member's declared personal credits.

    The credits are non-refundable: they can reduce tax to zero and no further.
    Each member's federal and Quebec credits are therefore summed and capped at
    that member's own pre-credit tax for the year (the figure the prologue taxed
    them on), exactly as ``tuition_credit`` caps its own credit -- Canada has no
    joint filing, so one spouse's credit can never reduce the other's tax.

    Returns True when any credit was applied, False for a household that
    declares no claims (all fields stay at their seeded 0.0 -> the golden
    invariant is unchanged by construction).
    """
    from countries.canada.tax_calc import (
        medical_expense_credit,
        charitable_donation_credit,
        political_contribution_credit,
    )

    config = ctx.config
    year = ctx.calendar_year
    province = config.province

    # The credit amounts and thresholds are year-versioned and only defined for
    # a real tax year; a direct unit-test caller that omits calendar_year gets
    # the projection INDEX here, which is not a tax year.
    if year < 2024:
        return False

    provider = ctx.tax_provider if ctx.tax_provider is not None else default_tax_provider()
    qc_lowest = _quebec_lowest_rate(provider, year, province)

    if province == "quebec":
        from countries.canada.provinces.quebec.quebec_credits import (
            quebec_charitable_donation_credit,
            quebec_medical_expense_credit,
        )

    def _credits_for(member: dict, taxable_income: float) -> float:
        by_year = member.get("claims_by_year")
        if not by_year:
            return 0.0
        claims = by_year.get(year)
        if not claims:
            return 0.0

        medical = claims.get("medical_expenses", 0.0)
        donations = claims.get("charitable_donations", 0.0)
        political = claims.get("political_contributions_federal", 0.0)
        dues = claims.get("union_dues", 0.0)

        credit = 0.0
        if dues > 0:
            # Union and professional dues are a DEDUCTION from income (ITA
            # s.8(1)(i)), not a credit, so they are worth the tax on the slice
            # of taxable income they remove -- bracket-fill, not the marginal
            # rate applied flat -- exactly as rules_leverage values the
            # s.20(1)(c) deduction. The cash the member paid is not modelled as
            # an outflow, on the same convention the credits above follow: the
            # household's declared living costs carry it, and what the run
            # reports changing is the TAX. Zero for a member who declares none.
            from tax_calculator import deduction_value
            credit += deduction_value(
                taxable_income, dues, ctx.year_brackets)
        if medical > 0:
            credit += medical_expense_credit(
                medical, taxable_income, year, provider=provider)
        if donations > 0:
            credit += charitable_donation_credit(
                donations, taxable_income, year, provider=provider)
        if political > 0:
            credit += political_contribution_credit(
                political, year, provider=provider)
        if province == "quebec":
            if medical > 0:
                credit += quebec_medical_expense_credit(
                    medical, taxable_income, qc_lowest, year, provider=provider)
            if donations > 0:
                credit += quebec_charitable_donation_credit(
                    donations, year, provider=provider)
        return credit

    members = config.family_members
    primary_member = next((m for m in members if m.get("role") == "primary"), {})
    spouse_member = next((m for m in members if m.get("role") == "spouse"), {})

    primary_credit = _credits_for(primary_member, ctx.primary_taxable_income)
    spouse_credit = _credits_for(spouse_member, ctx.spouse_taxable_income)

    # Non-refundable: never more than the member's own tax for the year.
    primary_applied = min(primary_credit, max(0.0, ctx.primary_tax_before))
    spouse_applied = min(spouse_credit, max(0.0, ctx.spouse_tax_before))

    ws.personal_credit_applied_primary = primary_applied
    ws.personal_credit_applied_spouse = spouse_applied
    return primary_applied > 0.0 or spouse_applied > 0.0
