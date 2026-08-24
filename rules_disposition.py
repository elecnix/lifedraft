"""Disposition rules: a home leaves the balance sheet, and the tax settles.

``property_disposition`` (#956 bite B -- a declared mid-horizon sale of a
NON-principal property) and ``principal_disposition`` (#956 bite E -- the
principal residence, which flows via house_value/mortgage/HELOC rather than
``config.properties`` and so cannot be sold by bite B's rule).

They share the disposition arithmetic: ``_disposition_gain_tax`` (gain banded
against the owners' taxable income, apportioned by the principal-residence
designation) and ``_disposition_cca_recapture_tax`` (a rental's UCC recapture).
Keeping the two rules with the one shared tax core is what makes the
conservation identity -- assets drop by EXACTLY selling costs + tax -- a single
reviewable claim (DP#9: one spelling, not two).

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from rule_registry import RuleContext, YearWorkingState, rule


# Issue #956 bite B (sale-core, DP#10/#26): the pure disposition arithmetic a
# declared mid-horizon property SALE settles in its sale year. Kept as a
# standalone helper so the conservation identity (the spec's crux) is directly
# unit-testable: the household's assets drop by EXACTLY the selling friction
# (selling_costs + disposition tax), the equity converted to investable cash
# less the friction that genuinely left the household. Pure (DP#3): a function
# of the property's appreciated gross value, its secured mortgage, its ACB,
# the PRE designation, the selling costs, the owner's taxable income (for
# gain banding), and the year's brackets -- nothing read off ``self``.
#
# The conservation identity (verified by tests/test_issue_956_bite_b_sale_core):
#   P_gross  = value_share * (1+appreciation_rate)^(sale_year - owned_from)
#             (or value_share when no appreciation_rate -- the SAME value
#             simulation_state._property_equity_for_year's appreciation branch
#             computes, so a property sold at its appreciated value realizes
#             exactly the equity it would have contributed had it been held);
#   E       = P_gross - secured_share      (the equity on the balance sheet);
#   T       = capital-gains tax on (P_gross - acb_share), apportioned by the
#             PRE taxable_fraction, banded against the owner's taxable income;
#   P_net   = P_gross - secured_share - selling_costs - T;
#   Δtotal_assets (sell vs hold, in the sale year) = P_net - E = -(selling_costs + T).
# Money is conserved: the equity converted to investable cash, less the
# friction that genuinely left the household (third-party costs + government
# tax). The money-conservation invariant (trajectory_invariants.py) passes.
def _disposition_gain_tax(
        gain: float, sale: Dict[str, Any], cal_year: int, brackets: list,
        primary_taxable_income: float, spouse_taxable_income: float,
) -> float:
    """The PRE-apportioned, per-owner-banded capital-gains tax on a
    disposition's ``gain`` (proceeds - ACB, 100%) -- the SHARED spine both
    ``_property_disposition_for`` (Bite B, non-principal) and
    ``_principal_disposition_for`` (Bite E, principal) compose (DP#9: one
    spelling of the gain-banding math, not two near-clones).

    The gain is apportioned by the PRE ``taxable_fraction`` (ITA s.40(2)(b)):
    a property designated for its whole ownership -> ~0 tax; a property with
    no designation -> fully taxable (``taxable_fraction = 1.0``). The
    designation is capped at the sale year (a property sold mid-horizon cannot
    designate years after its sale). The denominator is the FAMILY window
    (issue #969) -- the span across ALL the couple's designated properties --
    when the sale carries ``family_pre_window`` (a household with >=2
    designated properties), so a second property's gain is priced against the
    same family horizon the estate path uses, not its own span in isolation
    (which over-shelters it). Absent ``family_pre_window`` (the common
    one-property household, or no designation declared anywhere) the
    property's OWN designation span is the denominator -- byte-identical to
    the single-property path (DP#9: one spelling of the family window, shared
    with ``input_contract._family_pre_window``).

    Canada has no joint filing, so a jointly-owned property's gain is split
    per owner (``owner_roles`` -- each role's fraction is its declared % of
    the property; the fractions sum to ``couple_share``, so weighting the
    couple-share gain by ``role_frac/couple_share`` recovers each owner's
    share), and each share bands against THAT owner's taxable income (the
    marginal rate the gain lands in). Reuses
    ``estate.tax_on_capital_gain_at_death`` exactly (DP#9).

    DP#25: ``countries.canada`` is imported lazily inside this helper (this
    module keeps no jurisdiction import at top level).
    """
    if gain <= 0.0:
        return 0.0
    from countries.canada.pre_designation import (
        designated_years, taxable_gain_fraction)
    periods = sale.get('designated_principal_residence_years', [])
    designated = {y for y in designated_years(periods, cal_year) if y <= cal_year}
    designated_count = len(designated)
    if designated_count <= 0:
        taxable_fraction = 1.0
    else:
        # Issue #969: the FAMILY window (span across all the couple's
        # designated properties) is the denominator when the sale carries it
        # -- the same family horizon `input_contract._family_pre_window`
        # computes and the estate path prices against. Absent it (a single-
        # property household, or no designation anywhere) the property's OWN
        # designation span is the denominator -- byte-identical to the prior
        # single-property path (DP#9/DP#32). An explicit `is None` test, never
        # `or` (DP#32: 0 is a real window value the family-window helper only
        # returns when >=2 properties designate, so a 0 here is genuine).
        family_window = sale.get('family_pre_window')
        if family_window is None:
            window = (max(designated) - min(designated) + 1) if designated else 0
        else:
            window = family_window
        taxable_fraction = taxable_gain_fraction(designated_count, window)
    from countries.canada.estate import tax_on_capital_gain_at_death
    owner_roles = sale.get('owner_roles', {})
    couple_share = sum(owner_roles.values())
    if couple_share <= 0.0:
        return 0.0
    disposition_tax = 0.0
    for role, frac in owner_roles.items():
        if frac <= 0.0:
            continue
        # Each owner's share of the couple's gain: frac/couple_share recovers
        # the owner's share of the property (a couple owning 50/50 -> each
        # owner's gain share is half the couple's gain).
        owner_gain_share = gain * (frac / couple_share)
        other_income = (primary_taxable_income if role == 'primary'
                        else spouse_taxable_income)
        disposition_tax += tax_on_capital_gain_at_death(
            fmv=owner_gain_share, acb=0.0, brackets=brackets,
            other_income=other_income, inclusion_rate=0.5,
            taxable_fraction=taxable_fraction)
    return disposition_tax


def _disposition_cca_recapture_tax(
        prop: Dict[str, Any], p_gross: float, cal_year: int, brackets: list,
        primary_taxable_income: float, spouse_taxable_income: float,
        opening_rental_ucc: Dict[str, float]) -> float:
    """The CCA recapture / terminal-loss tax on a sold rental that elected
    CCA (issue #968, ITA s.13(1) / s.20(16)) -- the disposition-side clawback
    the estate path's ``objective._cca_recapture_for`` assumes this rule prices
    (see issue #964: a sold rental is skipped at the deemed disposition BECAUSE
    its recapture was realized here, at the sale).

    A rental that claimed CCA depreciated a declining-balance UCC below its
    capital cost. On sale, proceeds up to the original capital cost that EXCEED
    the remaining UCC are RECAPTURED as ordinary income (100% inclusion -- taxed
    WORSE than the 50%-inclusion capital gain on the same property); the
    symmetric case -- UCC still above the (cost-capped) proceeds -- is a
    TERMINAL LOSS deductible against ordinary income. Recapture and terminal
    loss are mutually exclusive (at most one is non-zero).

    The recapture is ORDINARY income, so -- unlike the capital gain in
    ``_disposition_gain_tax`` (50% inclusion, banded via
    ``tax_on_capital_gain_at_death``) -- it is taxed at each owner's marginal
    rate stacked on top of their EXISTING taxable income (the incremental
    ``tax_on_income(other_income + share) - tax_on_income(other_income)``, the
    same stacking idiom the gain-band helper uses). A terminal loss is the
    mirror: a tax SAVING at the owner's marginal rate (a negative incremental
    tax, floored at zero overall so a loss in a no-tax year yields no refund
    here -- the engine does not model loss carry-back/carry-forward).

    DP#9: reuses ``countries.canada.cca.recapture_on_disposition`` (the SAME
    primitive the estate path consumes) -- one spelling of the recapture math.
    Consumes ``recapture``/``terminal_loss`` only, NEVER its ``capital_gain``:
    the gain is taxed separately by ``_disposition_gain_tax`` on the (p_gross -
    acb_share) base, and re-adding it here would double-tax (the primitive
    returns ``capital_gain`` only for symmetry).

    Proceeds for the recapture ceiling are ``p_gross`` (the couple's
    appreciated gross value at the sale year -- the SAME figure the estate path
    uses via ``cca['fmv_at_disposition']`` at death, so a mid-horizon sale and
    a deemed disposition share one property valuation). The UCC is the couple's
    OPENING UCC for the sale year (``opening_rental_ucc[prop_id]``, the UCC
    immediately before the mid-year disposition -- the closing UCC of the prior
    year, threaded through ``jurisdiction_state``); a property whose CCA ledger
    has no carried balance falls back to its declared ``opening_ucc`` (the
    first-year case), mirroring ``objective._cca_recapture_for``'s fallback
    exactly (DP#9).

    The couple-level recapture/terminal-loss is split per owner in the SAME
    ``owner_roles`` proportion the gain is split (``frac/couple_share``
    recovers each owner's share of the property), and each share bands against
    THAT owner's taxable income. Returns 0.0 for a non-rental sale or a rental
    without a CCA election (the absence-safe no-op, DP#32 -- a rental that
    never elected CCA has nothing to recapture).
    """
    rental = prop.get('rental')
    cca = rental.get('cca') if rental else None
    if not cca:
        return 0.0
    sale = prop['sale']
    owner_roles = sale.get('owner_roles', {})
    couple_share = sum(owner_roles.values())
    if couple_share <= 0.0:
        return 0.0
    prop_id = prop['id']
    ucc = opening_rental_ucc.get(prop_id, cca['opening_ucc'])
    from countries.canada.cca import recapture_on_disposition
    conseq = recapture_on_disposition(
        p_gross, cca['capital_cost'], ucc)
    recapture = conseq['recapture']
    terminal_loss = conseq['terminal_loss']
    if recapture <= 0.0 and terminal_loss <= 0.0:
        return 0.0
    from tax_calculator import tax_on_income
    tax = 0.0
    for role, frac in owner_roles.items():
        if frac <= 0.0:
            continue
        owner_frac = frac / couple_share
        other_income = (primary_taxable_income if role == 'primary'
                        else spouse_taxable_income)
        if recapture > 0.0:
            # 100%-inclusion ordinary income stacked on the owner's return.
            share = recapture * owner_frac
            tax += (tax_on_income(other_income + share, brackets)
                    - tax_on_income(other_income, brackets))
        if terminal_loss > 0.0:
            # A deductible terminal loss: a marginal tax saving (a negative
            # contribution). Floored per-owner at the tax already on the
            # owner's return so a loss in a no-tax year yields no refund here
            # (the engine does not model loss carry-back/carry-forward).
            share = terminal_loss * owner_frac
            base_tax = tax_on_income(other_income, brackets)
            saving = base_tax - tax_on_income(max(0.0, other_income - share),
                                               brackets)
            tax -= min(saving, base_tax)
    return tax


def _property_disposition_for(
        prop: Dict[str, Any], cal_year: int, start_year: int,
        brackets: list, primary_taxable_income: float, spouse_taxable_income: float,
        opening_rental_ucc: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Compute one sold property's disposition figures for ``cal_year`` (its
    sale year): ``p_gross``, ``p_net``, ``disposition_tax``, ``realized_gain``,
    and the per-owner gain shares. Returns zeros when ``cal_year`` is not this
    property's sale year (a strict no-op for every other year and every
    property held to the horizon -- DP#32)."""
    sale = prop.get('sale')
    if sale is None or sale['year'] != cal_year:
        return {'p_gross': 0.0, 'p_net': 0.0, 'disposition_tax': 0.0,
                'realized_gain': 0.0}
    if opening_rental_ucc is None:
        opening_rental_ucc = {}  # a direct unit-test caller (no UCC ledger)
    # P_gross: the appreciated gross value at the sale year, the SAME value
    # _property_equity_for_year's appreciation branch computes (so a sold
    # property realizes exactly the equity it would have contributed held).
    # owned_from = the purchase year if a dated purchase, else start_year
    # (mirrors _property_equity_for_year exactly -- DP#9, one spelling).
    value_share = prop['value_share']
    rate = prop.get('appreciation_rate')
    purchase = prop.get('purchase')
    owned_from = purchase['year'] if purchase is not None else start_year
    if rate is None or rate == 0.0:
        p_gross = value_share
    else:
        years_held = max(0, cal_year - owned_from)
        p_gross = value_share * ((1.0 + rate) ** years_held)
    acb_share = prop['acb_share']
    secured_share = prop['secured_share']
    selling_costs = sale['selling_costs']

    # PRE-apportioned, per-owner-banded capital-gains tax on the realized
    # gain (shared spine, DP#9 -- one spelling of the gain-banding math).
    # Issue #140: the gain is SIGNED. The pre-#140 ``max(0.0, ...)`` floor
    # turned a disposition below ACB into a phantom $0 gain -- a realized
    # capital loss was unrepresentable, so it could never join the
    # s.111(1)(b) carry-forward pool (which ``sale_realized_gain`` feeds).
    # The TAX still clamps at its own edge: ``tax_on_capital_gain_at_death``
    # floors the taxable base at 0 internally, so a loss-year sale books
    # zero tax (a negative tax would be a fabricated refund) while the
    # signed loss flows to the ledger.
    realized_gain = p_gross - acb_share
    disposition_tax = _disposition_gain_tax(
        realized_gain, sale, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income)
    # Issue #968: CCA recapture on a rental SALE. A rental that claimed CCA
    # owes recapture (ITA s.13(1), 100%-inclusion ordinary income) -- real tax
    # the v1 scope skipped for lack of the sale-year UCC. The opening UCC is
    # now threaded onto YearWorkingState (the UCC immediately before the
    # mid-year disposition), so the recapture/terminal-loss is priced here,
    # on top of the capital-gain tax above. Consumes recapture/terminal_loss
    # only, never the primitive's capital_gain (the gain path above already
    # taxes it -- DP#9). 0.0 for a non-rental sale or a rental with no CCA
    # election (absence-safe, DP#32).
    disposition_tax += _disposition_cca_recapture_tax(
        prop, p_gross, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income, opening_rental_ucc)
    p_net = p_gross - secured_share - selling_costs - disposition_tax
    return {'p_gross': p_gross, 'p_net': p_net,
            'disposition_tax': disposition_tax, 'realized_gain': realized_gain}

@rule('property_disposition')
def apply_property_disposition(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #956 bite B (sale-core, DP#10/#26): a declared mid-horizon
    voluntary SALE of a property settles in its sale year.

    When a property declaring ``sale`` reaches its sale calendar year, its net
    proceeds (``P_net`` = gross appreciated value less the secured mortgage
    discharged, the selling costs, and the disposition tax) are INVESTED into
    the non-registered account (establishing fresh ACB), the realized capital
    gain is surfaced for reporting, and the disposition tax is surfaced for
    transparency. The property's equity gated to zero this year
    (``simulation_state._property_equity_for_year``); the proceeds replace it
    in the portfolio.

    Conservation (the spec's crux): ``Δtotal_assets = P_net - E =
    -(selling_costs + T)`` -- the household's assets drop by EXACTLY the
    selling friction (third-party costs + government tax). Money is conserved:
    the equity converted to investable cash, less the friction that genuinely
    left the household. The money-conservation invariant passes (the sale is
    an asset-class swap, cash-flow-neutral -- the P_gross received exactly
    funds the mortgage discharge, the friction, and the invested P_net, so the
    solvency identity is unaffected: no inflow or outflow is needed).

    Wiring: the proceeds are injected POST-GROWTH (added to
    ``ws.new_nonreg_bal`` / ``ws.new_nonreg_acb`` by this rule, which runs
    AFTER ``non_reg_growth`` and BEFORE ``solvency``) rather than via
    ``alloc.non_reg`` (the year-0 free_cash path). The free_cash path adds the
    contribution to the OPENING balance, which then compounds in the sale year
    -- that would make the year-N drop ``P_net*(1+r) - E`` (rate-dependent),
    NOT the exact ``-(selling_costs + T)`` the conservation identity requires.
    Post-growth injection adds P_net at the END of the sale year (after
    compounding), so the year-N drop is exactly the friction and P_net "then
    grows" from the NEXT year on (the spec's "investable non-reg that then
    grows"). Fresh ACB = P_net (the invested cash establishes its own cost
    basis, DP#19). The sale is absence-safe (DP#32): a household with no sale
    (incl. the golden fixture) has every property's ``sale`` absent -> the
    rule is a strict no-op -> the golden invariant is unchanged by
    construction.

    DP#25: ``countries.canada`` is imported lazily inside the helper (this
    module keeps no jurisdiction import at top level). The capital-gains tax
    is computed once, here (in ``T``); it is NOT also added to the year's
    ordinary income-tax base (that would double-tax). The realized gain is
    surfaced on ``YearResult.realized_capital_gains`` (mirroring
    ``realized_capital_gains``, issue #754) for the year-end AMT base (#710)
    and reporting.

    Issue #968: ``T`` also carries the CCA RECAPTURE (and any terminal-loss
    saving) when the sold property is a rental that elected CCA -- 100%-
    inclusion ordinary income (ITA s.13(1)) priced on the opening UCC at the
    sale year (threaded onto ``ws.opening_rental_ucc``), split per owner and
    banded against each owner's taxable income. The estate path skips a sold
    rental's recapture at the deemed disposition (issue #964) BECAUSE this
    rule prices it here; before #968 that skip left the recapture taxed
    nowhere. The recapture is real tax leaving the household, so it enters
    ``T`` and the conservation identity holds exactly as before
    (``Δtotal_assets = -(selling_costs + T)`` -- T now includes the
    recapture). 0.0 for a non-rental sale or a rental with no CCA election
    (absence-safe, DP#32).

    Returns True when any property is sold this year (the rule had an
    observable effect), False for a year with no sale (no-op, all outputs stay
    at their seeded 0.0 defaults).
    """
    config = ctx.config
    cal_year = ctx.calendar_year
    # Absence-safe fast path (DP#32): a household with no property sold this
    # year (every property's `sale` absent or in a different calendar year --
    # incl. the golden fixture and every pre-/post-sale year) is a strict
    # no-op. Brackets are only needed to band a gain, so a no-sale year does
    # not require them -- a direct unit-test caller that did not pass
    # year_brackets still runs cleanly.
    props_in_sale_year = [
        prop for prop in getattr(config, 'properties', [])
        if prop.get('sale') is not None
        and prop['sale']['year'] == cal_year
    ]
    if not props_in_sale_year:
        return False

    brackets = ctx.year_brackets
    if brackets is None:
        # A property IS sold this year but no brackets were passed to band the
        # gain against -- a caller error, not a silent zero (DP#32); raise
        # rather than under-tax the gain. The prologue resolves these for the
        # marginal rates, so the live fold always passes them.
        raise ValueError(
            "property_disposition rule needs ctx.year_brackets to band the "
            "disposition gain; got None (the prologue resolves these for the "
            "marginal rates -- a direct caller must pass them too).")

    total_p_net = 0.0
    total_tax = 0.0
    total_gain = 0.0
    for prop in props_in_sale_year:
        figures = _property_disposition_for(
            prop, cal_year, config.start_year, brackets,
            ctx.primary_taxable_income, ctx.spouse_taxable_income,
            ws.opening_rental_ucc)
        p_net = figures['p_net']
        # Inject the net proceeds into non-reg POST-GROWTH (this rule runs
        # after non_reg_growth), establishing fresh ACB. The proceeds replace
        # the property's equity in the portfolio; they compound from NEXT
        # year on (the conservation identity holds exactly in the sale year).
        ws.new_nonreg_bal += p_net
        ws.new_nonreg_acb += p_net
        total_p_net += p_net
        total_tax += figures['disposition_tax']
        total_gain += figures['realized_gain']

    ws.sale_proceeds_invested = total_p_net
    ws.sale_disposition_tax = total_tax
    ws.sale_realized_gain = total_gain
    return True

def _principal_disposition_for(
        sale: Dict[str, Any], cal_year: int,
        mortgage_balance: float, heloc_balance: float, sm_heloc: float,
        brackets: list, primary_taxable_income: float,
        spouse_taxable_income: float,
        start_year: Optional[int] = None) -> Dict[str, float]:
    """Compute the principal residence's disposition figures for ``cal_year``
    (its sale year): ``discharged_debt``, ``disposition_tax``,
    ``realized_gain``, ``p_net``. Returns all zeros when ``cal_year`` is
    before the sale year (the rule's pre-sale no-op -- the home is held,
    nothing is discharged, no proceeds). When ``cal_year`` is the sale year OR
    any year after it, the debt is discharged (the home + its mortgage + any
    HELOC/SM leave the balance sheet from the sale year on -- the rule
    force-zeros the secured debt every post-sale year so the amortization
    schedule's scheduled balance does not resurrect a paid-off mortgage); the
    proceeds, tax, and gain are priced ONCE, in the sale year only.

    The principal's value is NOT in ``total_assets`` (it flows via
    ``house_value`` / LTV / charge math, not via ``property_equities``), so
    the discharged debt is the LIVE year-N balance read off
    ``YearWorkingState`` (``new_mortgage_balance`` / ``new_heloc_balance`` /
    ``new_sm_heloc``), not a config-time snapshot -- the principal's mortgage
    AMORTIZES in the engine (unlike a non-principal property's static
    ``secured_share``), so a snapshot would under-state the retired debt and
    break the conservation identity. The gain (``value_share - acb_share``)
    is apportioned by the PRE taxable_fraction (ITA s.40(2)(b)) and banded
    against each owner's taxable income, reusing
    ``estate.tax_on_capital_gain_at_death`` exactly (DP#9 -- one spelling of
    the gain-banding math, not a second). ``P_net = value_share -
    discharged_debt - selling_costs - T``.

    Issue #963 (epic #956 bite F): when the sale carries an
    ``appreciation_rate`` (the principal declared one), the sale realizes
    the APPRECIATED gross value at the sale year -- ``value_share`` compounds
    at ``(1 + rate) ** (syear - start_year)`` (the principal is held from
    the projection start, so the ownership year IS ``start_year``). The
    ``acb_share`` stays at cost (appreciation does not change ACB -- DP#19),
    so the realized gain grows by exactly the accrued appreciation.
    ``start_year`` is required ONLY when appreciation is declared; a sale
    with no rate never reads it (absence-safe, DP#32).
    """
    syear = sale['year']
    if cal_year < syear:
        # Pre-sale: the home is held, nothing is discharged, no proceeds.
        return {'discharged_debt': 0.0, 'disposition_tax': 0.0,
                'realized_gain': 0.0, 'p_net': 0.0}
    # The home + its mortgage + any HELOC/SM secured against it leave the
    # balance sheet from the sale year on. The discharged debt is the LIVE
    # year-N balance (the mortgage amortizes; a config snapshot would be
    # wrong). The SM HELOC (readvance line) is secured against the same
    # charge and is discharged alongside the mortgage.
    discharged_debt = mortgage_balance + heloc_balance + sm_heloc
    if cal_year > syear:
        # Post-sale year: the debt is already 0 (discharged last year and
        # carried forward at 0), but the rule force-zeros it again anyway so
        # the amortization schedule's scheduled `end_balance` does not
        # resurrect a paid-off mortgage. No proceeds/tax/gain to price (the
        # sale settled once, in its sale year).
        return {'discharged_debt': discharged_debt, 'disposition_tax': 0.0,
                'realized_gain': 0.0, 'p_net': 0.0}
    # ── The sale year: price the disposition ONCE. ──
    # Issue #963 (epic #956 bite F): when the principal declares an
    # `appreciation_rate`, the sale realizes the APPRECIATED gross value at
    # the sale year, not the static base `value_share` -- a downsize/sell of
    # a grown home realizes the growth. The base compounds at
    # `(1 + rate) ** (syear - start_year)` (the principal is held from the
    # projection start, so the ownership year IS `start_year`). Absence-safe
    # (DP#32): an absent or 0.0 rate returns `value_share` unchanged and never
    # reads the exponent, so a sale with no appreciation is byte-identical to
    # bite E. `acb_share` stays at cost -- appreciation does not change ACB
    # (DP#19), so the realized gain grows by exactly the accrued appreciation.
    value_share = sale['value_share']
    rate = sale.get('appreciation_rate')
    if rate is not None and rate != 0.0:
        # `start_year` is the principal's ownership year (the projection
        # start -- the principal is never a dated mid-horizon purchase). It
        # is required to compound the base; a caller that declares
        # appreciation MUST pass it (the rule does, from ctx.config.start_year).
        if start_year is None:
            raise ValueError(
                "_principal_disposition_for needs start_year to compound the "
                "principal's appreciation_rate (declared on the sale); got None. "
                "The rule passes ctx.config.start_year; a direct caller must "
                "too (DP#32).")
        years = max(0, syear - start_year)
        value_share = value_share * ((1.0 + rate) ** years)
    acb_share = sale['acb_share']
    selling_costs = sale['selling_costs']
    # PRE-apportioned, per-owner-banded capital-gains tax on the realized gain
    # (the SHARED spine, DP#9 -- one spelling of the gain-banding math, the
    # same helper Bite B's _property_disposition_for composes). A principal
    # designated for its whole ownership -> ~0 tax; a principal with no
    # designation -> fully taxable.
    # Issue #140: this floor is JUSTIFIED BY STATUTE, not by omission. A
    # principal-residence disposition's accrued "gain" is pre-apportioned
    # here (the PRE machinery below), and under ITA s.40(2)(b) a loss on a
    # principal residence is NEVER an allowable capital loss -- Parliament
    # exempted PR gains and expressly denied the symmetric loss relief. So a
    # below-ACB principal sale is genuinely a $0-tax / no-loss event; letting
    # this figure go negative would fabricate a deduction statute forbids.
    # (Non-principal property sales realize their SIGNED gain -- see
    # _property_disposition_for above.)
    realized_gain = max(0.0, value_share - acb_share)
    disposition_tax = _disposition_gain_tax(
        realized_gain, sale, cal_year, brackets,
        primary_taxable_income, spouse_taxable_income)
    p_net = value_share - discharged_debt - selling_costs - disposition_tax
    return {'discharged_debt': discharged_debt,
            'disposition_tax': disposition_tax,
            'realized_gain': realized_gain, 'p_net': p_net}

@rule('principal_disposition')
def apply_principal_disposition(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #956 bite E (principal-residence disposition, DP#10/#26): a
    declared mid-horizon SALE of the PRINCIPAL residence settles in its sale
    year.

    The principal residence is deliberately excluded from
    ``config.properties`` (its value reaches the annual side via
    ``house_value`` / LTV / charge math, not via ``property_equities`` in
    ``total_assets``), so Bite B's ``property_disposition`` rule cannot sell
    it -- the principal's sale is a separate disposition with its own rule.
    When the principal's declared ``sale`` reaches its sale calendar year,
    the home + its mortgage + any HELOC/SM secured against it LEAVE the
    balance sheet: this rule force-zeros ``new_mortgage_balance``,
    ``new_heloc_balance``, and ``new_sm_heloc`` (so the amortization
    schedule's scheduled `end_balance` does not resurrect a paid-off
    mortgage, and the SM readvance line is retired), and the net proceeds
    ``P_net = value_share - discharged_debt - selling_costs - T`` are
    INVESTED into the non-registered account POST-GROWTH (establishing fresh
    ACB), the realized gain is surfaced for reporting, and the disposition
    tax is surfaced for transparency. The SM INVESTMENT (``new_sm_investment``)
    is NOT zeroed -- it is a real asset that stays (the loan is discharged,
    the asset remains, ACB unchanged); only its financing is retired.

    Conservation (the spec's gate): the principal's value is NOT in
    ``total_assets`` (it flows via ``house_value`` / charge math, off the
    balance sheet), so the conservation identity is on NET_ASSETS, not
    ``total_assets`` -- distinct from Bite B's non-principal identity (the
    non-principal's equity IS in ``total_assets``, so Bite B's
    ``Δtotal_assets = -(selling_costs + T)`` holds). For the principal:

      Δtotal_assets (sell vs hold, sale year) = P_net
        (the proceeds invested -- the hold household has 0 proceeds);
      Δtotal_debt   (sell vs hold, sale year) = -(mortgage + heloc + sm_heloc)
        (the discharged secured debt -- the hold household keeps it);
      Δnet_assets   (sell vs hold, sale year) = P_net + discharged_debt
        = value_share - selling_costs - T
        = V - selling_costs - T

    -- the home's gross value realized, less the friction that genuinely
    left the household (third-party selling costs + government tax). The
    home's equity (V - debt) enters net_assets for the first time (as
    proceeds + debt retired), minus the friction. Money is conserved: the
    off-balance-sheet home converts to on-balance-sheet assets, less the
    friction that genuinely left the household. For a fully-PRE-designated
    principal (the common case), T ≈ 0, so Δnet_assets ≈ V - selling_costs.
    V is the principal's APPRECIATED value at the sale year when the
    household declares an ``appreciation_rate`` (#963 bite F), else the
    static ``value_share`` (byte-identical to bite E).

    Wiring: the proceeds are injected POST-GROWTH (added to
    ``ws.new_nonreg_bal`` / ``ws.new_nonreg_acb`` by this rule, which runs
    AFTER ``non_reg_growth`` -- so P_net does not compound in the sale year,
    and the year-N identity holds exactly) and BEFORE ``solvency`` (so the
    invested non-reg is on the balance sheet the waterfall reads). The rule
    runs AFTER ``mortgage`` / ``margin_heloc_interest`` / ``sm_readvance`` /
    ``sm_investment_growth`` / ``heloc_interest_servicing`` so the
    HELOC/SM interest, readvance, and growth rules have set their ``new_*``
    values and the SM investment (a real asset that STAYS) has grown; it
    runs BEFORE ``rrsp_refund_heloc_paydown`` (no HELOC to pay down
    post-sale). The secured debt is force-zeroed EVERY year from the sale
    year on (the amortization schedule keeps producing a scheduled
    ``end_balance`` -- the rule overrides it every post-sale year so a
    paid-off mortgage stays paid off).

    The sale is absence-safe (DP#32): a household with no principal sale
    (incl. the golden fixture, which builds SimulationConfig.from_dict
    straight from a legacy dict that never carries ``principal_sale``) has
    ``config.principal_sale`` is None -> the rule is a strict no-op -> the
    golden invariant is unchanged by construction.

    DP#25: ``countries.canada`` is imported lazily inside the helper (this
    module keeps no jurisdiction import at top level). The capital-gains tax
    is computed once, here (in ``T``); it is NOT also added to the year's
    ordinary income-tax base (that would double-tax). The realized gain is
    surfaced on ``YearResult.realized_capital_gains`` (mirroring the
    drawdown/solvency/Bite-B realized gains, issue #754) for the year-end
    AMT base (#710) and reporting.

    Returns True when the principal is in or past its sale year (the rule
    had an observable effect -- the debt is force-zeroed), False for a year
    before the sale (no-op, all outputs stay at their seeded 0.0 defaults).
    """
    sale = ctx.config.principal_sale
    # Absence-safe fast path (DP#32): a household with no principal sale
    # (incl. the golden fixture and every pre-sale year) is a strict no-op.
    if sale is None:
        return False
    cal_year = ctx.calendar_year
    if cal_year < sale['year']:
        # Pre-sale: the home is held, nothing is discharged, no proceeds.
        return False

    brackets = ctx.year_brackets
    if brackets is None and cal_year == sale['year']:
        # The principal IS sold this year but no brackets were passed to band
        # the gain -- a caller error, not a silent zero (DP#32); raise rather
        # than under-tax the gain. The no-sale / pre-sale / post-sale paths do
        # NOT need brackets (no gain to band) and do not raise.
        raise ValueError(
            "principal_disposition rule needs ctx.year_brackets to band the "
            "disposition gain in the sale year; got None (the prologue "
            "resolves these for the marginal rates -- a direct caller must "
            "pass them too).")

    figures = _principal_disposition_for(
        sale, cal_year, ws.new_mortgage_balance, ws.new_heloc_balance,
        ws.new_sm_heloc, brackets if brackets is not None else [],
        ctx.primary_taxable_income, ctx.spouse_taxable_income,
        start_year=ctx.config.start_year)
    discharged_debt = figures['discharged_debt']
    # Force-zero the discharged secured debt: the home + its mortgage + any
    # HELOC/SM secured against it leave the balance sheet from the sale year
    # on. The mortgage amortization schedule keeps producing a scheduled
    # `end_balance` -- this override every post-sale year keeps a paid-off
    # mortgage paid off (the carried-forward opening balance is 0, and the
    # schedule's end_balance is overridden to 0 here).
    ws.new_mortgage_balance = 0.0
    ws.new_heloc_balance = 0.0
    ws.new_sm_heloc = 0.0
    # The SM INVESTMENT (new_sm_investment) is NOT zeroed -- it is a real
    # asset that stays (the loan is discharged, the asset remains, ACB
    # unchanged). Its interest deduction stops (sm_interest prices
    # new_sm_heloc * rate = 0 next year on), and it compounds as a regular
    # non-reg holding from the sale year on.
    ws.principal_sale_discharged_debt = discharged_debt
    if cal_year == sale['year']:
        # ── The sale year: invest the net proceeds POST-GROWTH, surface
        # the gain + tax. non_reg_growth already ran (this rule sits after
        # it), so P_net does not compound in the sale year -- the year-N
        # conservation identity holds exactly. Fresh ACB = P_net (DP#19).
        p_net = figures['p_net']
        ws.new_nonreg_bal += p_net
        ws.new_nonreg_acb += p_net
        ws.principal_sale_proceeds_invested = p_net
        ws.principal_sale_disposition_tax = figures['disposition_tax']
        ws.principal_sale_realized_gain = figures['realized_gain']
    # Post-sale years: only the debt-zeroing fires (above) -- no proceeds,
    # tax, or gain to price (the sale settled once, in its sale year).
    return True
