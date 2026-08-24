"""The ``solvency`` rule: was any of it affordable, and what had to be sold.

Dead last but one in ``RULE_ORDER`` (only the year-end AMT assessment follows):
the only rule that checks whether everything every earlier rule booked was
actually payable out of this year's cash flow, sizes the emergency reserve, and
may force-liquidate through the waterfall to close a shortfall.

The outflow-side pure helpers it composes live here with it: the dated,
finite-term living-cost segments (#760) and their seasonal monthly vector
(#882), a mid-horizon property purchase's cash outflow (#696), the recurring
carrying cost of owning a non-principal property (#1010), and the locked
(not-yet-liquid) balance a pot cannot contribute (#823).

Split out of ``simulation_rules.py``; the rule body is unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List

from liquidation_waterfall import months_covered, reserve_target
from rule_registry import RuleContext, YearWorkingState, rule


def _still_locked(ctx: 'RuleContext', kind: str) -> float:
    """Issue #823: the total balance still locked (not yet liquid) for the
    ``kind`` pot in ``ctx.calendar_year``.

    Sums the ``balance`` of every account of ``kind`` that declared
    ``locked_until`` whose owner has NOT yet reached the account's
    ``unlock_age`` this year. After the owner passes the unlock age the
    balance is liquid and contributes 0 here. Returns 0.0 when no account of
    ``kind`` declared ``locked_until`` (today's fully-liquid behaviour,
    golden). DP#1: the owner's age is ``calendar_year - owner_birth_year``.
    """
    locked = ctx.config.account_locked
    entries = locked.get(kind) if locked else None
    if not entries:
        return 0.0
    year = ctx.calendar_year
    total = 0.0
    for e in entries:
        owner_age = year - e.get('owner_birth_year', year)
        if owner_age < e.get('unlock_age', 0):
            total += e.get('balance', 0.0)
    return total

# Issue #760: the ANNUAL amount a dated, finite-term living-cost segment
# contributes in one calendar year, as a module-level pure function (DP#3) so
# apply_solvency has ONE spelling of the day-count blend. A segment is an
# annual recurring amount active over the half-open window [from, to); for a
# given calendar year Y it contributes ``amount * (days of [from, to) ∩ Y) /
# (days in Y)`` -- the outflow-side analog of #674's dated income windows.
# ``to = None`` means perpetual (active through year-end and beyond); a
# non-null ``to`` means the expense ENDS there and contributes $0 from that
# date on (never carried to the horizon the way annual_living_costs is). A
# segment whose window does not touch Y at all (before ``from``, or on/after a
# non-null ``to``) contributes 0.0. Leap years are exact (days-in-year is
# 365 or 366, from the calendar), so the blend never approximates to a flat
# 1/365.
def _property_purchase_outflow_in_year(config, calendar_year: int) -> float:
    """Issue #696 (epic #690 bite 5): the total cash outflow the household's
    dated property purchases require in ``calendar_year`` -- the couple's down
    payment (``net_equity``) plus its share of ``closing_costs`` for each
    property whose ``purchase.year`` is this year. Zero in every other year and
    for every property held from year 0 (no ``purchase``), so a household with
    no mid-horizon purchase is byte-for-byte today (DP#32). The purchased
    property's equity enters the balance sheet the same year
    (``simulation_state._property_equity_for_year``); charging the down payment
    here draws it from the liquidation waterfall, conserving money (DP#18)."""
    total = 0.0
    for prop in config.properties:
        purchase = prop.get('purchase')
        if purchase is not None and purchase['year'] == calendar_year:
            total += prop['net_equity'] + purchase['closing_costs']
    return total

# Issue #1010 (epic #956): the ANNUAL recurring carrying-cost outflow a
# NON-principal property charges in ``calendar_year`` -- property tax,
# maintenance, insurance, condo/HOA -- the holding cost of owning the home,
# modelled as a per-property attribute the way ``rental_facts.expenses_annual``
# models a rental's T776 operating expense. A module-level pure function
# (DP#3) so apply_solvency has ONE spelling of the ownership-window gate.
#
# Active over the OWNERSHIP WINDOW: from the purchase year if the property was
# bought mid-horizon (#696), else the projection ``start_year`` (held from year
# 0, the #692 path), THROUGH the sale year (#956 bite B): a property sold mid-
# horizon charges its carrying cost UP TO but not including the sale year (the
# property leaves the balance sheet in the sale year and its net proceeds
# replace it -- there is nothing left to carry). The gate mirrors
# ``_property_equity_for_year`` exactly (purchase-then-sale ordering, so a sold
# property charges nothing in its sale year regardless of appreciation) --
# DP#9, one spelling of the ownership window.
#
# The outflow is the couple's share of the flat ``annual_amount`` (already scaled
# at map time by ``couple_share``, mirroring ``purchase.closing_costs``) PLUS
# ``fraction_of_value`` applied to the couple's share of the property's CURRENT
# (appreciating) gross value. The gross value compounds at ``appreciation_rate``
# from the ownership year exactly as ``_property_disposition_for`` prices a
# sale's ``P_gross`` (``value_share * (1+rate)**years_held``), so a carrying
# cost declared as a % of value tracks the appreciating asset, not a static
# snapshot -- the composition with appreciation the issue asks for.
#
# Absence-safe (DP#32): a property with no ``carrying_costs`` (the #692/#696/
# #956 default, incl. the golden fixture) returns 0.0 and never reads
# ``value_share``/``appreciation_rate`` -- so a household that declares no
# carrying cost is byte-identical to today. A declared ``{}`` (both components
# null) is a real $0 and also returns 0.0. ``fraction_of_value == 0.0`` is a
# real zero (0% of value) and is skipped, never coerced via ``or``. The flat
# ``annual_amount == 0`` (a real zero) adds 0.0 and is read explicitly.
def _property_carrying_cost_in_year(prop: Dict[str, Any],
                                    calendar_year: int,
                                    start_year: int) -> float:
    cc = prop.get('carrying_costs')
    if cc is None:
        return 0.0
    purchase = prop.get('purchase')
    sale = prop.get('sale')
    # Ownership window -- mirrors _property_equity_for_year's gate exactly
    # (DP#9): not yet owned before the purchase year, gone from the sale year
    # on (nothing left to carry in the sale year itself).
    owned_from = purchase['year'] if purchase is not None else start_year
    if calendar_year < owned_from:
        return 0.0
    if sale is not None and calendar_year >= sale['year']:
        return 0.0
    total = 0.0
    amount = cc.get('annual_amount')
    if amount is not None:
        total += amount
    fraction = cc.get('fraction_of_value')
    if fraction is not None and fraction != 0.0:
        # The couple's share of the CURRENT gross value, appreciating from the
        # ownership year (the SAME compounding _property_disposition_for uses
        # for a sale's P_gross). value_share is carried by the adapter whenever
        # fraction_of_value is non-null (input_contract._map_owned_properties),
        # so it is present here by construction.
        value_share = prop['value_share']
        rate = prop.get('appreciation_rate')
        if rate is None or rate == 0.0:
            gross = value_share
        else:
            years_held = max(0, calendar_year - owned_from)
            gross = value_share * ((1.0 + rate) ** years_held)
        total += fraction * gross
    return total


def _total_carrying_cost_in_year(config, calendar_year: int, start_year: int) -> float:
    """The aggregate recurring carrying-cost outflow across every non-principal
    property in ``calendar_year`` -- the loop wrapper apply_solvency calls, so
    the rule cites ONE function (DP#9). Zero for a household that declares no
    carrying costs on any property, byte-for-byte today (DP#32)."""
    total = 0.0
    for prop in config.properties:
        total += _property_carrying_cost_in_year(prop, calendar_year, start_year)
    return total

def _expense_segment_contribution_in_year(segment: Dict[str, Any],
                                          calendar_year: int) -> float:
    from datetime import date
    # Issue #882: an OPTIONAL intra-year SEASONALITY. When the segment declares
    # ``active_months`` (a subset of 1..12), its ANNUAL ``amount`` is spent in
    # equal shares across ONLY those months (heating that runs Nov-Mar, a
    # property-tax bill paid each July) and the within-year pattern is resolved
    # at MONTHLY granularity -- the year's charge is the sum of the active
    # months touched by the [from, to) window. Absent the key the segment is
    # active every day of the window and the untouched day-count blend below
    # runs, byte-for-byte #760 (DP#32 no-op when undeclared).
    active_months = segment.get('active_months')
    if active_months is not None:
        return sum(_expense_segment_monthly_outflows(segment, calendar_year))
    start = segment['from']
    if not isinstance(start, date):
        start = date.fromisoformat(start)
    end = segment['to']
    if end is not None and not isinstance(end, date):
        end = date.fromisoformat(end)
    year_start = date(calendar_year, 1, 1)
    next_year_start = date(calendar_year + 1, 1, 1)  # exclusive upper bound
    win_start = max(start, year_start)
    win_end = next_year_start if end is None else min(end, next_year_start)
    if win_end <= win_start:
        return 0.0
    days_active = (win_end - win_start).days
    days_in_year = (next_year_start - year_start).days
    return segment['amount'] * days_active / days_in_year


# Issue #882: the 12-length vector (one entry per calendar month) of the amount
# a SEASONAL segment charges in ``calendar_year``, at monthly resolution.
# ``amount`` is the ANNUAL figure split evenly over the declared active months;
# a month contributes its ``amount / len(active_months)`` share prorated by the
# days of that month falling inside the [from, to) window (so a boundary month
# the window enters or leaves mid-way stays day-exact, and an active month
# before ``from`` or on/after a non-null ``to`` contributes 0). Summed, this is
# the year's charge (``_expense_segment_contribution_in_year``); its PEAK entry
# sizes the emergency reserve (``apply_solvency``) so a concentrated seasonal
# bill is bridged at its peak month, not its annual average. Only called for a
# segment that declares ``active_months`` -- there is no monthly vector for a
# non-seasonal segment (its cost is a continuous day-count, not a monthly one).
def _expense_segment_monthly_outflows(segment: Dict[str, Any],
                                      calendar_year: int) -> List[float]:
    from datetime import date
    active_months = segment['active_months']
    start = segment['from']
    if not isinstance(start, date):
        start = date.fromisoformat(start)
    end = segment['to']
    if end is not None and not isinstance(end, date):
        end = date.fromisoformat(end)
    per_month = segment['amount'] / len(active_months)
    outflows = [0.0] * 12
    for month in active_months:
        month_start = date(calendar_year, month, 1)
        month_end = (date(calendar_year + 1, 1, 1) if month == 12
                     else date(calendar_year, month + 1, 1))
        win_start = max(month_start, start)
        win_end = month_end if end is None else min(month_end, end)
        if win_end <= win_start:
            continue
        days_active = (win_end - win_start).days
        days_in_month = (month_end - month_start).days
        outflows[month - 1] = per_month * days_active / days_in_month
    return outflows

@rule('solvency')
def apply_solvency(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """The cash-flow identity (issue #679), enforced every year, and what
    actually happens when it fails.

    ``after_tax_income + drawdowns >= debt_service + living_costs +
    contributions`` must hold. Every earlier rule in this fold clamps
    contributions to available ROOM (RRSP/TFSA limits) -- never to
    affordability. This rule is the one place that asks whether the money
    for everything already booked this year actually existed, and if it
    did not, forces a real liquidation to cover the difference, in a fixed
    order, each with its real cost (``liquidation_waterfall.py``):
    emergency reserve -> revolving credit facility -> non-registered
    (capital-gains tax, realised losses reported honestly) -> TFSA ->
    registered (fully taxable, at whatever this year's rate is -- the
    worst possible time, not a chosen one).

    Runs LAST in ``RULE_ORDER``: it must see the post-growth,
    post-contribution, post-drawdown state every other rule in this fold
    produced, because it may itself further reduce those very balances to
    fund a shortfall none of them checked for.

    DP#16: a no-op (module not engaged) when ``ctx.living_costs <= 0`` --
    the household's own budget was never supplied. This is NOT the DP#32
    zero-as-fallback trap: no real household spends $0/year to live, so a
    caller that never sets ``household_budget.annual_living_costs`` and one
    that (nonsensically) declared it as exactly 0 are indistinguishable in
    a way that matters -- there is no real "$0 living costs" case this
    engine needs to represent, unlike OAS or a savings rate, which
    genuinely can be zero. Absence of the input is absence of the
    constraint (exactly the LTV explorer's house_value/mortgage_balance/
    margin_available trigger-data pattern), not a value being silently
    coerced.

    If even the full waterfall cannot cover the shortfall, the year is
    marked ``ws.solvency_ruined = True`` -- a hard ruin, reported on
    ``YearResult.ruined``, never silently absorbed (DP#32). This function
    does not decide what a caller does with a ruined year; it only refuses
    to pretend the shortfall did not happen.

    **Issue #689 closed the representability gap** (the waterfall's second
    step used to be structurally $0: ``personal_loan`` forbade a ``limit``
    and required an ``amortization``; ``heloc`` required collateral -- a
    revolving, unsecured, interest-only credit facility could not be written
    into the input contract at all). ``liabilities[kind=line_of_credit]`` now
    maps onto ``SimulationConfig.credit_facility_limit``, and this rule draws
    it -- BEFORE any asset below it -- whenever the household is short. A
    household that has not declared one still sets
    ``ws.solvency_credit_facility_unrepresentable = True`` on a shortfall
    year, so the report can say the household's real resilience MAY be
    understated by an undeclared real-world facility -- that is a fact about
    the INPUT now, not a structural gap in the engine.

    Deliberately NOT the same facility as the mortgage-paired HELOC
    (``margin_available``/``heloc_balance``): a HELOC is a *secured* product
    carved out of the same registered charge as the mortgage (#664/#681),
    and its balance's tax deductibility is TRACED by use (ITA s.20(1)(c),
    ``countries/canada/debt.py``). Drawing it here, for a personal shortfall,
    would poison that tracing and model a different product than the one
    being asked about. The credit facility this rule draws is its own thing
    -- secured or not (``SimulationConfig.credit_facility_secured``), priced
    at its own declared rate, with no investment-purpose tracing at all.

    Interest on whatever is drawn CAPITALIZES onto the balance every
    subsequent year (this rule computes it every year, not only shortfall
    years, so a balance drawn in year N keeps compounding in year N+1 even
    if N+1 is solvent) -- a documented simplification (DP#30): this facility
    is a rarely-touched backstop, not the household's primary borrowing
    instrument, and the engine already services HELOC interest in cash where
    that distinction actually changes strategy rankings (issue #681).
    """
    # issue #763: the cash-flow identity's debt-service term is BOTH the
    # mortgage payment AND the closed-end consumer loans' payment -- a car
    # payment the household must make is the same kind of essential outflow
    # the mortgage payment is, and the #758 reserve/runway target is sized
    # against the same combined figure (SimState.initial sizes the year-0
    # reserve off _annual_debt_service + _annual_consumer_loan_service).
    # issue #759: the installment plan's payment joins the SAME non-
    # compressible debt-service channel -- a contractual payment plan the
    # household is legally bound to is exactly the essential outflow a
    # reserve/runway exists to bridge, and it does NOT compress under an
    # income shock (contrast #761's discretionary split, which compresses
    # annual_living_costs). It is added here, beside the consumer-loan
    # payment, so the identity's debt-service term and the reserve sizing
    # see all three must-pay outflows at once.
    # Issue #967: the second-property mortgage's payment joins the SAME
    # non-compressible debt-service channel -- a mortgage the household must
    # service is exactly the essential outflow the principal mortgage, consumer
    # loans, and installments already are, and the #758 reserve/runway target
    # is sized against the same combined figure.
    consumer_debt_service = (ws.consumer_loan_payment + ws.installment_payment
                             + ws.second_property_mortgage_payment)
    # Issue #760: dated, finite-term living-cost segments layered ON TOP OF the
    # perpetual `living_costs` scalar (a private-school tuition that ENDS when a
    # child ages out, childcare, a term expense that stops). Each active segment
    # contributes its ANNUAL amount prorated by the days of [from, to) falling
    # in this calendar year (day-count blend -- the outflow-side analog of
    # #674's dated income windows; simulation_rules._expense_segment_
    # contribution_in_year), and STOPS after a non-null `to` (reverts to $0,
    # never carried to the horizon). Split by each segment's own
    # non_discretionary flag: a non-discretionary segment (tuition, childcare)
    # is a must-pay essential outflow that sizes the reserve and never
    # compresses; a discretionary one (non_discretionary=false) compresses to
    # zero under an income shock exactly as #761's scalar split does -- so the
    # flag lands on a real decision (DP#18) and its two values are genuinely
    # distinct (DP#32). Empty `expense_segments` -> both totals 0.0, byte-for-
    # byte today's behaviour (the golden invariant does not move).
    seg_nondiscretionary = 0.0
    seg_discretionary = 0.0
    # Issue #882: a per-month total of the NON-discretionary seasonal segments'
    # outflow, so the reserve can be sized on the PEAK month rather than the
    # annual average. Stays all-zero unless a segment declares active_months --
    # a non-seasonal segment has no monthly vector (DP#32 no-op when undeclared).
    seasonal_nondiscretionary_monthly = [0.0] * 12
    for _seg in ctx.config.expense_segments:
        _contribution = _expense_segment_contribution_in_year(_seg, ctx.calendar_year)
        if _contribution <= 0.0:
            continue
        if _seg['non_discretionary']:
            seg_nondiscretionary += _contribution
            if _seg.get('active_months') is not None:
                for _i, _amt in enumerate(
                        _expense_segment_monthly_outflows(_seg, ctx.calendar_year)):
                    seasonal_nondiscretionary_monthly[_i] += _amt
        else:
            seg_discretionary += _contribution
    # A discretionary segment compresses to zero under a dated income shock
    # (income_shock_active is False in retirement -- it is a working-life
    # detection -- so retirement charges every active segment in full).
    seg_discretionary_compressed = seg_discretionary if ctx.income_shock_active else 0.0
    seg_charged = seg_nondiscretionary + (seg_discretionary - seg_discretionary_compressed)
    ws.expense_segment_outflow = seg_charged
    ws.emergency_reserve_target = reserve_target(
        ctx.config.emergency_reserve_target_months,
        # Issue #760: a committed dated expense (tuition, childcare) is an
        # ESSENTIAL outflow the reserve must bridge; the discretionary portion
        # is compressible and does not inflate the reserve.
        annual_living_costs=ctx.living_costs + seg_nondiscretionary,
        annual_debt_service=ws.mort.get('total_payment', 0.0) + consumer_debt_service,
    )
    # Issue #882 (monthly-resolution reserve sizing): reserve_target above spreads
    # each non-discretionary segment's annual amount evenly over 12 months. For a
    # SEASONAL segment the year's cost concentrates into a few months, so the
    # reserve must bridge the PEAK month, not the average. Add the excess of the
    # peak seasonal month over that evenly-spread average, scaled by the same
    # target-months policy. For a non-seasonal household the monthly vector is
    # all zero -> premium 0.0 -> byte-for-byte #760 (DP#32 no-op when undeclared).
    _reserve_months = ctx.config.emergency_reserve_target_months
    if _reserve_months is not None and _reserve_months > 0 and any(
            seasonal_nondiscretionary_monthly):
        _peak = max(seasonal_nondiscretionary_monthly)
        _average = sum(seasonal_nondiscretionary_monthly) / 12.0
        ws.emergency_reserve_target += _reserve_months * (_peak - _average)
    # Issue #689: carries the credit facility's balance forward (with
    # interest capitalized) EVERY year, including years this rule otherwise
    # no-ops below (``living_costs`` undeclared, or no shortfall) -- a
    # balance drawn in an earlier shock year must not evaporate just because
    # a later year is solvent.
    ws.new_credit_facility_balance = ws.opening_credit_facility_balance * (
        1.0 + (ctx.config.credit_facility_rate or 0.0)
    )
    ws.emergency_reserve_months_covered = months_covered(
        ws.new_emergency_reserve,
        # Issue #760: same essential-outflow base as the reserve target above.
        annual_living_costs=ctx.living_costs + seg_nondiscretionary,
        annual_debt_service=ws.mort.get('total_payment', 0.0) + consumer_debt_service,
    )

    if ctx.living_costs <= 0:
        return False

    debt_service = ws.mort.get('total_payment', 0.0) + consumer_debt_service
    contributions = (
        ws.p_rrsp_actual + ws.s_rrsp_actual + ws.sp_rrsp_actual
        + ws.p_tfsa_actual + ws.sp_tfsa_actual + ws.non_reg_alloc
        + ws.resp_alloc + ws.fhsa_actual
    )
    # Issue #696 (epic #690 bite 5): a dated mid-horizon PROPERTY PURCHASE is a
    # cash outflow in its purchase year, funded like every other spend the
    # household cannot cover from income -- through THIS identity's liquidation
    # waterfall (this year's income surplus first, then a real account draw),
    # NOT a silent reduction of that year's contributions (which clamps at zero
    # and would let a down payment larger than the year's savings vanish,
    # inventing net worth). The outflow is the couple's DOWN PAYMENT (net_equity
    # = value less the mortgage originated against it) plus the couple's share
    # of CLOSING COSTS (both mapped by input_contract._map_owned_properties).
    # Money is conserved (DP#18): the whole outflow is charged to the identity
    # and sourced from real inflows/assets, never clamped away -- the same
    # discipline the mortgage payment and #760's dated expense segments already
    # have. The property's equity enters the balance sheet the SAME year
    # (simulation_state._property_equity_for_year): the down-payment dollars come
    # off the asset side (portfolio) and reappear as equity, while the closing
    # costs are a pure expense. It is a non-compressible, non-discretionary
    # one-time outflow (a signed purchase, not a lifestyle choice), so it does
    # not compress under an income shock. Zero in every year but a purchase year,
    # and for every property held from year 0 (no `purchase`) -> byte-for-byte
    # today's behaviour (DP#32).
    purchase_outflow = _property_purchase_outflow_in_year(
        ctx.config, ctx.calendar_year)
    # Issue #1010 (epic #956): a non-principal property's RECURRING carrying
    # costs (property tax, maintenance, insurance) are an annual cash outflow
    # over the ownership window, funded like every other spend the household
    # cannot cover from income -- through THIS identity's liquidation waterfall
    # (this year's income surplus first, then a real account draw), the SAME
    # discipline the #696 purchase outflow and #760 dated expense segments
    # already have. Money is conserved (DP#18): the whole outflow is charged to
    # the identity and sourced from real inflows/assets, never clamped away.
    # It is a non-compressible, non-discretionary recurring outflow (property
    # tax is a must-pay, not a lifestyle choice), so it does not compress under
    # an income shock. Zero in every year outside the ownership window and for
    # every property declaring no carrying costs (incl. the golden fixture) ->
    # byte-for-byte today's behaviour (DP#32).
    carrying_cost_outflow = _total_carrying_cost_in_year(
        ctx.config, ctx.calendar_year, ctx.config.start_year)
    # Borrowed money that was invested this year is an INFLOW as well as an
    # outflow: it arrives from the lender and leaves for the brokerage in the
    # same breath. `contributions` below already counts the outflow, so the
    # inflow must be counted too or the identity invents a shortfall that
    # never existed (see RuleContext.borrowed_investment). The DEBT it created
    # is not forgiven -- it is booked on the balance sheet and serviced, at
    # interest, in `debt_service` and the HELOC-interest rules, every year
    # after this one. That is where a leveraged strategy is made to pay.
    #
    # Issue #758 (the retirement double-count): in WORKING life the identity
    # charges the working-phase `living_costs` and counts after-tax
    # EMPLOYMENT income -- a shortfall there is a shock-induced cash-flow
    # crisis (what runway measures). In RETIREMENT the household funds its
    # spending BY drawing down assets (#707's drawdown), and the drawdown is
    # sized to `retirement_spending_target - (CPP+OAS+pension)`. Charging the
    # working `living_costs` here on top of the drawdown double-counts
    # spending: the household spends `retirement_spending_target` (usually
    # LOWER than the working budget), and CPP/OAS/pension -- netted out of the
    # drawdown's sizing -- are not added back to `available` (ctx.after_tax
    # _income is employment income only). The old form force-sold assets every
    # retirement year to cover spending that does not happen, corrupting the
    # solvency table, #707's decumulation output, and #758's runway.
    #
    # The correct retirement identity: charge `retirement_spending_target`
    # (what the drawdown is sized to) and count CPP+OAS+pension in available.
    # Then available = retirement_income + drawdown = spending_target, so
    # shortfall = debt_service (+ contributions) -- the REAL mortgage gap
    # only, no double-count, money still conserved (the waterfall funds the
    # real gap). #707 owns the "drawdown could not deliver the target" case.
    if ws.any_retired:
        # Issue #760: a dated segment still active in retirement (e.g. a
        # committed expense spanning into it) is a real outflow ON TOP OF the
        # retirement spending target -- charged in full (income_shock_active is
        # False in retirement, so nothing compresses). A segment that already
        # ended (`to` in an earlier year) contributes 0 here by construction.
        spending_outflow = (ws.retirement_spending_target + seg_charged
                            + purchase_outflow + carrying_cost_outflow)
        available = (ctx.after_tax_income + ws.drawdown_net_delivered
                     + ws.cpp_income + ws.oas_income + ws.pension_income
                     + ctx.borrowed_investment + ctx.free_cash_invested
                     # Issue #1001: the forced RRIF minimum's after-tax slice
                     # that funds the net spending target (priced in
                     # apply_retirement_income, split in apply_rrif_minimum).
                     # The discretionary drawdown is now sized to the residual
                     # shortfall AFTER this slice, so available still equals
                     # retirement_spending_target (money conserved). 0.0 in
                     # every pre-RRIF year and any year no forced minimum fires.
                     + ws.rrif_after_tax_to_spending
                     # Issue #967: a mid-horizon mortgage's ORIGINATED
                     # principal is an INFLOW in the purchase year (the
                     # mortgage funds the purchase: it arrives from the lender
                     # and leaves for the seller in the same breath, the same
                     # inflow==outflow discipline borrowed_investment uses).
                     # 0.0 in every year but a financed property's purchase
                     # year, and for a household with no financing (DP#32).
                     + ws.second_property_mortgage_originated
                     # epic #795 bite 3: the tuition_credit rule's per-member
                     # tax reduction (the credit reduces tax, so it raises
                     # after-tax income). 0.0 for a household that declares
                     # no tuition (the golden path) -- a strict no-op.
                     + ws.tuition_credit_applied_primary
                     + ws.tuition_credit_applied_spouse
                     # Issue #1083: the s.20(1)(c) deduction's statutory saving
                     # on the primary's prologue-taxed rental/loan slice -- the
                     # tax the prologue already embedded in
                     # ``ctx.after_tax_income`` is lower by this amount, so it
                     # is added back here (the tuition_credit booking path).
                     # 0.0 unless the primary is retired AND the deduction
                     # exceeds the cpp+pension drawdown base (the golden
                     # household and every accumulation year: strict no-op).
                     + ws.sm_interest_nondrawdown_tax_saving)
        discretionary_compressed = seg_discretionary_compressed
    else:
        # Issue #761: under a dated INCOME SHOCK a household cuts DISCRETIONARY
        # spending first (restaurants, vacations, entertainment) and keeps
        # funding the non-discretionary core (groceries, utilities, insurance,
        # transport, medicine) plus debt service and committed installments
        # (#759). When the household declared household_budget.discretionary_fraction
        # AND this year's income is reduced below baseline by a decisions.income[]
        # override (ctx.income_shock_active), the identity charges only the
        # NON-discretionary portion; the discretionary portion is compressed
        # to ZERO -- a labelled, documented stress assumption (a household in
        # genuine distress stops dining out and taking vacations; it does not
        # stop buying groceries). The compression is reported on YearResult so
        # the assumption is visible, never silently absorbed (DP#32).
        #
        # No split declared (discretionary_fraction is None) OR no shock this
        # year -> spending_outflow is the full scalar, byte-for-byte today's
        # behaviour. The model_fidelity.runway_treats_all_spend_as_rigid
        # approximation fires in that case to name the "all rigid" assumption
        # explicitly; it is suppressed when a split IS declared, so the output
        # always states which assumption it is making.
        discretionary_compressed = 0.0
        frac = ctx.config.discretionary_fraction
        if (frac is not None and ctx.income_shock_active
                and 0.0 <= frac <= 1.0):
            discretionary_compressed = ctx.living_costs * frac
        # Issue #760: the dated segments' charged amount (net of any
        # discretionary segment compressed under this year's shock) is added on
        # top of the scalar spend; the compressed segment dollars join the
        # scalar's discretionary compression so YearResult reports the full
        # stress relief transparently (DP#32).
        spending_outflow = (
            (ctx.living_costs - discretionary_compressed) + seg_charged
            + purchase_outflow + carrying_cost_outflow)
        discretionary_compressed += seg_discretionary_compressed
        available = (ctx.after_tax_income + ws.drawdown_net_delivered
                     + ctx.borrowed_investment + ctx.free_cash_invested
                     # Issue #967: a mid-horizon mortgage's ORIGINATED
                     # principal is an INFLOW in the purchase year (the
                     # mortgage funds the purchase). 0.0 otherwise (DP#32).
                     + ws.second_property_mortgage_originated
                     # epic #795 bite 3: the tuition_credit rule's per-member
                     # tax reduction (0.0 for a no-tuition household).
                     + ws.tuition_credit_applied_primary
                     + ws.tuition_credit_applied_spouse)
    required = debt_service + spending_outflow + contributions

    # epic #795 bite 3: ctx.after_tax_income is the PRE-credit after-tax
    # income the prologue now passes (the credit moved into the registered
    # tuition_credit rule, which writes the per-member tax reduction to ws).
    # Add the reduction back so YearResult.after_tax_income reports the
    # POST-credit figure byte-identical to the pre-refactor prologue output
    # (the characterization test pins it per year). 0.0 for a no-tuition
    # household, so the golden invariant is unchanged.
    ws.solvency_after_tax_income = (
        ctx.after_tax_income
        + ws.tuition_credit_applied_primary
        + ws.tuition_credit_applied_spouse
        # Issue #1083: the s.20(1)(c) nondrawdown routing's saving is a tax
        # reduction on income already inside ``ctx.after_tax_income`` -- report
        # the POST-saving figure, exactly as the tuition credits above report
        # the POST-credit one. 0.0 for every household/phase the routing does
        # not reach (the golden invariant is untouched).
        + ws.sm_interest_nondrawdown_tax_saving)
    # The reported `living_costs` field stays the declared working-phase
    # budget (what the household declared); only the identity's `required`
    # uses `spending_outflow` (the retirement target in retirement years, or
    # the non-discretionary portion under a shock when a split was declared)
    # so the field's semantics don't drift. Issue #761: the spending figure
    # actually charged and the discretionary dollars compressed are stamped
    # separately so the identity is transparent (DP#32).
    ws.solvency_living_costs = ctx.living_costs
    ws.solvency_spending_outflow = spending_outflow
    ws.solvency_discretionary_compressed = discretionary_compressed
    ws.solvency_debt_service = debt_service
    ws.solvency_contributions = contributions

    shortfall = required - available
    if shortfall <= 1e-9:
        return False

    from liquidation_waterfall import (
        LiquidationSource, capital_gains_cost, identity_cost,
        ordinary_income_cost, run_waterfall,
    )

    # Signed, NOT clamped to [0, 1] -- a forced sale while the account sits
    # below cost basis (the correlated job-loss/market-crash case issue
    # #679 is about) must report a genuine realised loss.
    non_reg_gain_frac = (
        (ws.new_nonreg_bal - ws.new_nonreg_acb) / ws.new_nonreg_bal
        if ws.new_nonreg_bal > 0 else 0.0
    )

    # Issue #689: the undrawn ROOM left on the credit facility, AFTER this
    # year's interest capitalization above -- $0 whenever no line_of_credit
    # was declared (credit_facility_limit defaults to 0.0, DP#32) and
    # correctly shrinking in a later shock year if an earlier one already
    # drew part of it. A household that never declared this facility gets
    # a reported, honest $0 here -- and the report says so explicitly below
    # -- rather than a silently invented one.
    revolving_credit_available = max(
        0.0, ctx.config.credit_facility_limit - ws.new_credit_facility_balance)
    ws.solvency_credit_facility_unrepresentable = ctx.config.credit_facility_limit <= 0

    # Issue #823: illiquidity -- balances whose account declared `locked_until`
    # are NOT liquid in any year the owner has not yet reached the unlock age,
    # so they are excluded from the solvency liquidation waterfall (and thus
    # from runway, which composes this verdict). After the owner's unlock age
    # the balance IS liquid. An empty `account_locked` map (no account
    # declared locked_until) excludes nothing -- today's fully-liquid
    # behaviour (golden). DP#1: the owner's age is date-computed from
    # calendar_year - owner_birth_year, never a hardcoded constant.
    locked_non_reg = _still_locked(ctx, 'non_reg')
    locked_tfsa = _still_locked(ctx, 'tfsa')
    locked_registered = _still_locked(ctx, 'rrsp') + _still_locked(ctx, 'spousal_rrsp')

    sources = [
        # Issue #688: the reserve is drawn FIRST, and it is the POST-growth
        # sleeve (apply_emergency_reserve_growth already compounded it at its
        # own declared cash rate this year), not the Jan-1 balance.
        LiquidationSource('emergency_reserve', ws.new_emergency_reserve, identity_cost),
        LiquidationSource('revolving_credit', revolving_credit_available, identity_cost),
        LiquidationSource('non_reg', max(0.0, ws.new_nonreg_bal - locked_non_reg),
                           capital_gains_cost(non_reg_gain_frac,
                                               ctx.config.capital_gains_inclusion,
                                               ctx.primary_marginal_rate)),
        LiquidationSource('tfsa', max(0.0, ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal - locked_tfsa), identity_cost),
        LiquidationSource('registered',
                           max(0.0, ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal - locked_registered),
                           ordinary_income_cost(ctx.primary_marginal_rate)),
    ]

    result = run_waterfall(shortfall, sources)
    drawn = {step.source: step.gross_drawn for step in result.steps}

    ws.new_emergency_reserve = max(
        0.0, ws.new_emergency_reserve - drawn.get('emergency_reserve', 0.0))
    ws.emergency_reserve_months_covered = months_covered(
        ws.new_emergency_reserve,
        annual_living_costs=ctx.living_costs,
        annual_debt_service=debt_service,
    )

    # Issue #689: whatever the waterfall drew from the credit facility this
    # year is now DEBT -- added to the balance already carried forward (with
    # this year's interest) above. It compounds, at credit_facility_rate,
    # every subsequent year via the capitalization step at the top of this
    # rule, exactly like margin_heloc_interest does for the HELOC.
    ws.new_credit_facility_balance += drawn.get('revolving_credit', 0.0)

    non_reg_drawn = drawn.get('non_reg', 0.0)
    if non_reg_drawn > 0:
        ws.new_nonreg_bal -= non_reg_drawn
        # DP#19 ACB bookkeeping mirrors the existing convention used
        # elsewhere in this fold (apply_retirement_drawdown, above): cost
        # basis is reduced dollar-for-dollar with the withdrawal amount,
        # floored at 0 -- not a proportional ACB reduction. The *reported*
        # realized_gain (liquidation_waterfall.capital_gains_cost, folded
        # into ws.solvency_realized_loss below) is still computed honestly
        # from the pre-withdrawal gain fraction; only the account's own
        # go-forward ACB bookkeeping follows the pre-existing convention.
        ws.new_nonreg_acb = max(0.0, ws.new_nonreg_acb - non_reg_drawn)

    tfsa_drawn = drawn.get('tfsa', 0.0)
    if tfsa_drawn > 0:
        tfsa_total = ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
        if tfsa_total > 0:
            p_share = tfsa_drawn * (ws.new_tfsa_p_bal / tfsa_total)
            ws.new_tfsa_p_bal = max(0.0, ws.new_tfsa_p_bal - p_share)
            ws.new_tfsa_sp_bal = max(0.0, ws.new_tfsa_sp_bal - (tfsa_drawn - p_share))

    registered_drawn = drawn.get('registered', 0.0)
    if registered_drawn > 0:
        reg_total = ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal
        if reg_total > 0:
            p_share = registered_drawn * (ws.new_rrsp_bal / reg_total)
            s_share = registered_drawn * (ws.new_spousal_rrsp_bal / reg_total)
            sp_share = registered_drawn - p_share - s_share
            ws.new_rrsp_bal = max(0.0, ws.new_rrsp_bal - p_share)
            ws.new_spousal_rrsp_bal = max(0.0, ws.new_spousal_rrsp_bal - s_share)
            ws.new_spouse_rrsp_bal = max(0.0, ws.new_spouse_rrsp_bal - sp_share)

    ws.solvency_shortfall = shortfall
    ws.solvency_covered = result.covered
    ws.solvency_tax_paid = sum(step.tax for step in result.steps)
    ws.solvency_realized_loss = sum(min(0.0, step.realized_gain) for step in result.steps)
    # Issue #754: the positive counterpart -- a forced non-reg liquidation above
    # cost base realizes a taxable capital gain, surfaced for the year-end AMT base.
    ws.solvency_realized_gain = sum(max(0.0, step.realized_gain) for step in result.steps)
    ws.solvency_liquidations = [
        {'source': step.source, 'gross_drawn': step.gross_drawn,
         'net_proceeds': step.net_proceeds, 'tax': step.tax,
         'realized_gain': step.realized_gain}
        for step in result.steps
    ]
    ws.solvency_ruined = result.ruined
    return True
