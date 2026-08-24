"""Leverage rules: borrowed money, its ITA s.20(1)(c) purpose, and its cost.

Purpose tracing (``heloc_tracing``, ``borrowing_purpose``), the Smith-Manoeuvre
sleeve (``sm_readvance``, ``sm_interest``, ``sm_investment_growth``,
``sm_unwind``), the revolving margin secured by the same charge
(``margin_heloc_interest``, ``heloc_interest_servicing``), and the RRSP-refund
paydown that repays it (``rrsp_refund_heloc_paydown``).

They belong together because they share one balance sheet item and one legal
question: how much is drawn against the charge, and how much of the interest on
it is deductible. ``_principal_value_for_year`` lives here because the charge
room is sized against the principal residence's appreciated gross value.

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

import logging

from simulation_config import SimulationConfig
# The OSFI B-20 charge geometry and its typed refusal moved out of
# simulation_config into their own module when the config was split (DP#25:
# one concept per module, and this one imports nothing).
from charge_limits import (
    charge_room_for_readvance,
    ReadvanceableWithoutPropertyError,
)
from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule

logger = logging.getLogger(__name__)


def _principal_value_for_year(config: SimulationConfig, cal_year: int) -> float:
    """Issue #963 (epic #956 bite F): the principal residence's GROSS value in
    calendar ``cal_year``, applying the declared ``appreciation_rate`` so the
    home's value compounds year over year exactly as Bite A does for a
    non-principal property.

    The principal's value is NOT in ``total_assets`` (it flows via
    ``house_value`` / LTV / charge math and, at the horizon, the estate's
    deemed disposition); this helper is the ONE spelling of the appreciated
    value every consumer of the principal's gross value reads (DP#9 -- one
    spelling, not a second per consumer): the annual LTV/charge room (the
    ``apply_sm_readvance`` / ``apply_margin_heloc_interest`` rules), Bite E's
    principal-sale gross (the ``apply_principal_disposition`` rule), and the
    estate's terminal FMV (``objective._estate_call_args``).

    The base value is ``config.house_value`` (the principal is held from the
    projection start -- never a dated mid-horizon purchase, so the ownership
    year IS ``config.start_year``), and the value at calendar year Y is
    ``house_value * (1 + rate) ** (Y - start_year)``. Absence-safe (DP#32): an
    absent or 0.0 rate returns the static ``house_value`` unchanged and never
    reads the exponent, so a household that declares no appreciation (incl.
    the golden fixture) is byte-identical to today. A negative rate is honored
    (the schema permits it; a falling market is a real scenario a sell/keep
    sweep must be robust to). """
    rate = config.appreciation_rate
    if rate is None or rate == 0.0:
        return config.house_value
    years = max(0, cal_year - config.start_year)
    return config.house_value * ((1.0 + rate) ** years)

@rule('heloc_tracing')
def apply_heloc_tracing(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Record this year's contribution advances into HELOC tracing buckets
    (ITA §20(1)(c) purpose test). Depends on ``contributions``.
    """
    opening = ws.opening_heloc_tracing
    added = ws.p_rrsp_actual + ws.s_rrsp_actual + ws.p_tfsa_actual + ws.sp_tfsa_actual + ws.non_reg_alloc
    ws.new_tracing = {
        'total_advances': opening.get('total_advances', 0) + added,
        'investment_advances': opening.get('investment_advances', 0) + ws.non_reg_alloc,
        'rrsp_advances': opening.get('rrsp_advances', 0) + ws.p_rrsp_actual + ws.s_rrsp_actual,
        'tfsa_advances': opening.get('tfsa_advances', 0) + ws.p_tfsa_actual + ws.sp_tfsa_actual,
        'personal_draws': opening.get('personal_draws', 0),
    }
    return added > 0

@rule('borrowing_purpose')
def apply_borrowing_purpose(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Trace the year-0 leveraged lump sum's two borrowings -- the mortgage
    ADVANCE and the DRAWN revolving margin -- to purpose, ITA s.20(1)(c)
    (issue #850). Depends on nothing; consumed by ``sm_interest``.

    #849's question ("advance or line?") is asked ENTIRELY because of a
    deductibility asymmetry, and before #850 the engine modelled it on neither
    leg: ``config.cash_out`` only ever sized the invested lump sum, and
    ``apply_margin_heloc_interest`` never deducted. This rule is where the two
    borrowings acquire a traced purpose, so ``apply_sm_interest`` can price
    them on the same deduction rule the readvance line already uses (DP#9 --
    the arithmetic is ``simulation_state.borrowing_purpose_tracings``, beside
    the ``margin_draw_for_lump_sum`` rule whose split it reuses, not a fourth
    copy here).

    Fires at YEAR 0 ONLY. The purpose of a borrowing is fixed when the money is
    spent; every later year carries the same trace forward, and it is the
    BALANCES priced against it that move. That is exactly what makes the
    erosion visible: a FIXED deductible proportion of an AMORTIZING mortgage is
    a falling deductible balance, while the same fixed proportion of a
    non-amortizing revolving balance is not.

    Inert for a household that borrowed no lump sum (``ws.lump_sum <= 0``) --
    the golden household included: both traces stay all-zero, so the deductible
    proportion is 0.0 and no deduction arises anywhere (DP#32).
    """
    if ws.year != 0 or ws.lump_sum <= 0:
        ws.new_advance_tracing = ws.opening_advance_tracing
        ws.new_margin_tracing = ws.opening_margin_tracing
        return False

    from simulation_state import borrowing_purpose_tracings
    ws.new_advance_tracing, ws.new_margin_tracing = borrowing_purpose_tracings(
        lump_sum=ws.lump_sum,
        lump_non_reg=ws.lump_non_reg,
        margin_available=ctx.config.margin_available,
        # The post-refinance charge this run opened with: the advance's blended
        # denominator (the pre-existing, personal-purpose balance plus the
        # advance itself). Read off the OPENING balance, before this year's
        # amortization -- the proportion is fixed at the moment of borrowing.
        mortgage_balance=ws.opening_mortgage_balance,
        # Issue #1039: ADD the new draw to a declared opening drawn balance's
        # trace instead of clobbering it -- the historical position's purpose
        # is a carried-in fact, not something a year-0 decision overwrites.
        opening_margin_tracing=ws.opening_margin_tracing,
    )
    return True

@rule('sm_readvance')
def apply_sm_readvance(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Readvanceable mortgage (DP#7: the mechanism, not the branded product):
    readvance this year's mortgage principal paydown into the HELOC and
    invest it -- BOUNDED BY THE REGISTERED CHARGE (issue #681).
    Depends on ``mortgage`` (principal_paid, new_mortgage_balance),
    ``margin_heloc_interest`` (new_heloc_balance, the drawn personal margin)
    and ``heloc_tracing`` (mutates the same tracing dict further).

    Issue #681: the readvance used to book ``principal_paid`` into the line
    unconditionally, every year, with no bound at all -- so a household
    re-borrowed its way to $3,057,904 of secured debt against an $800,000
    house (382% LTV), and the LESS it borrowed up front the MORE debt it
    ended with (a small mortgage amortizes faster -> more principal paid ->
    more readvanced, for longer). #664 bounded the year-0 *overlay* against
    the charge; nothing bounded the year-by-year *readvance*, so the
    constraint held at t=0 and was violated by t=1.

    The bound IS the mechanism (DP#7: model the mechanism, not the product).
    A readvanceable mortgage cannot lend past the charge registered against
    the property; the only reason paying principal down creates line room is
    that the charge is FIXED. So this year's draw is capped by whichever
    binds first:

      - the combined charge (OSFI B-20: 80% LTV for an uninsured combined
        loan plan) -- room = charge_limit - (mortgage + drawn revolving);
      - the revolving-only ceiling (OSFI B-20: 65% LTV for the readvanceable
        segment, independent of the 80% combined cap) -- room =
        heloc_revolving_limit - drawn revolving.

    Both are floored at zero: when the charge is full, the readvance STOPS.

    ``house_value`` is the principal's APPRECIATED value this year
    (``_principal_value_for_year``, issue #963 epic #956 bite F): a home that
    declares an ``appreciation_rate`` compounds at that rate, so a grown home
    has more collateral to re-borrow against. Absent/0.0 rate = the static
    ``config.house_value`` (byte-identical to today, DP#32) -- the
    conservative fixed-charge reading the contract states
    (``properties[].value``, an ``as_of``-dated fact) unless the household
    EXPLICITLY declares a rate (never a hardcoded default, DP#2/DP#13).
    """
    new_sm_heloc = ws.opening_readvance_heloc_balance
    new_sm_investment = ws.opening_sm_investment_balance
    new_sm_cost_basis = ws.opening_sm_investment_cost_basis
    fired = False

    # DP#32: a readvanceable line is a claim on a charge registered against a
    # PROPERTY. With no declared property value there is no charge, so this
    # rule cannot know what it is allowed to advance -- and it must say so,
    # not quietly advance nothing (a silently-zeroed readvance would look
    # exactly like a household that legitimately had no room) and not quietly
    # advance everything (the #681 bug: unbounded re-borrowing).
    if ctx.use_readvanceable and ctx.config.house_value <= 0:
        raise ReadvanceableWithoutPropertyError(
            "The readvanceable-mortgage strategy is active but "
            "config.house_value is 0 -- there is no declared property, so "
            "there is no registered charge, so the line's advanceable room "
            "is unknowable (issue #681). Declare property.house_value, or "
            "run with use_readvanceable=False. Refusing rather than "
            "advancing an unbounded amount against a property that was "
            "never stated (DP#32)."
        )

    readvance_room = charge_room_for_readvance(
        # Issue #963 (epic #956 bite F): the charge is registered against the
        # principal's APPRECIATED value this year, not the static `house_value`
        # -- a home that has grown has more collateral to re-borrow against
        # (mirrors Bite A's appreciated equity for a non-principal property).
        # Absence-safe: `_principal_value_for_year` returns the static
        # `house_value` when no rate is declared, byte-identical to today.
        house_value=_principal_value_for_year(ctx.config, ctx.calendar_year),
        mortgage_balance=ws.new_mortgage_balance,
        drawn_revolving=new_sm_heloc + ws.new_heloc_balance,
        charge_ltv_limit=ctx.config.charge_ltv_limit,
        heloc_ltv_limit=ctx.config.heloc_ltv_limit,
    )
    readvanced = min(ws.principal_paid, readvance_room)
    ws.readvance_room = readvance_room
    ws.readvance_blocked = max(0.0, ws.principal_paid - readvanced) if ctx.use_readvanceable else 0.0

    if ctx.use_readvanceable and readvanced > 0:
        from simulation_state import _new_heloc_tracing
        new_sm_heloc += readvanced
        new_sm_investment += readvanced
        new_sm_cost_basis += readvanced
        ws.new_tracing = _new_heloc_tracing(
            ws.new_tracing,
            total_advances=ws.new_tracing['total_advances'] + readvanced,
            investment_advances=ws.new_tracing['investment_advances'] + readvanced,
        )
        fired = True

    ws.sm_readvanced = readvanced if ctx.use_readvanceable else 0.0
    ws.new_sm_heloc = new_sm_heloc
    ws.new_sm_investment = new_sm_investment
    ws.new_sm_cost_basis = new_sm_cost_basis
    # These five are set unconditionally here (matching the original
    # engine's section boundary); `sm_interest` may overwrite the first
    # four and always sets/overwrites `new_qc_carry_forward`.
    ws.readvance_interest = 0.0
    ws.readvance_tax_savings = 0.0
    ws.deductible_proportion = 0.0
    ws.qc_deductible = 0.0
    ws.sm_interest_deduction = 0.0
    return fired

def _sm_schedule_l_income(ctx: RuleContext, pot_balances) -> float:
    """The year's TP-1 Schedule L net investment income (lines 2-6 total) of
    the non-reg pots the traced borrowings bought (#1035).

    Per pot: eligible + non-eligible dividends + interest/other + foreign
    (line 4's "other"), plus HALF the declared capital-gain yield component
    (line 5: only REALIZED taxable capital gains count, at the inclusion
    rate). The per-type rates come from the config's declared non-reg portfolio
    yield (DP#2/DP#27); when none is declared every component falls back to the
    configurable ``non_reg_yield_rate`` as interest -- reproducing the pre-
    #1035 ``balance * yield_rate`` base exactly (DP#32).

    Pure function of config + balances (DP#3): no fold state is read.
    """
    from countries.canada.portfolio import compute_investment_income
    yield_data = None
    portfolio_block = None if ctx.config is None else ctx.config.portfolio_data
    if isinstance(portfolio_block, dict):
        accounts = portfolio_block.get('accounts')
        if isinstance(accounts, dict):
            non_reg_acct = accounts.get('non_reg')
            if isinstance(non_reg_acct, dict):
                declared = non_reg_acct.get('yield')
                if isinstance(declared, dict):
                    yield_data = declared
    fallback_rate = 0.02 if ctx.config is None else ctx.config.non_reg_yield_rate
    total = 0.0
    for balance in pot_balances:
        if balance > 0:
            breakdown = compute_investment_income(
                balance, yield_data=yield_data,
                default_yield_rate=fallback_rate)
            # Schedule L line 5: capital gains enter at the inclusion rate.
            total += (breakdown['eligible_dividends']
                      + breakdown['non_eligible_dividends']
                      + breakdown['interest']
                      + breakdown['foreign_income']
                      + _CAPITAL_GAINS_INCLUSION * breakdown['capital_gains'])
    return total


_CAPITAL_GAINS_INCLUSION = 0.5  # Schedule L line 5 / ITA s.38 inclusion rate


def _year_split_brackets_for(ctx: RuleContext) -> tuple:
    """``(federal_slice, provincial_slice)`` brackets for this year, or
    ``(None, None)`` when the provider has no split for it -- the same
    warn-and-fallback contract ``simulation._year_brackets_for`` applies to
    the combined list (DP#20). Frozen-bracket runs resolve the split at the
    frozen start year, matching the combined list's year (DP#5).
    """
    if ctx.config is None:
        return None, None
    provider = (ctx.tax_provider if ctx.tax_provider is not None
                else default_tax_provider())
    cal_year = (ctx.config.start_year if ctx.config.frozen_brackets
                else ctx.calendar_year)
    try:
        return provider.get_split_brackets(cal_year,
                                           province=ctx.config.province)
    except ValueError:
        logger.warning(
            "No split tax bracket data for %s/%s; valuing the s.20(1)(c) "
            "deduction on the combined brackets (pre-#1035 behaviour).",
            cal_year, ctx.config.province)
        return None, None


@rule('sm_interest')
def apply_sm_interest(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """The §20(1)(c)-qualifying, QC-carry-forward-limited deductible interest
    on EVERY purpose-traced borrowing the household has. Depends on
    ``sm_readvance`` (new_sm_heloc), ``heloc_tracing`` (the readvance line's
    tracing ratio), and -- issue #850 -- on ``mortgage`` (the advance's
    interest and amortizing balance), ``margin_heloc_interest`` (the drawn
    line's interest and balance) and ``borrowing_purpose`` (both their
    tracings).

    ## The three legs

    Three DISTINCT secured balances can each carry deductible interest, and
    each has its own purpose trace (one trace per borrowing -- that IS the
    s.20(1)(c) tracing rule):

      1. ``new_sm_heloc`` -- the SM READVANCE line, traced by ``new_tracing``.
         The only leg this rule priced before #850.
      2. ``new_mortgage_balance`` -- the mortgage, including any cash-out
         ADVANCE, traced by ``new_advance_tracing`` (#850).
      3. ``new_heloc_balance`` -- the DRAWN revolving margin, traced by
         ``new_margin_tracing`` (#850).

    Legs 2 and 3 are #849's whole question. Before #850 neither was deducted
    anywhere, so "advance vs line" was ranked on the rate gap and interest
    capitalization alone -- a ranking of a different question than the one the
    household asked (DP#32). All three legs share ONE deductibility rule here
    and ONE proportion helper (``compute_heloc_deductible_proportion``, DP#9 --
    the acceptance criterion on #850 is explicitly "not a fourth copy").

    ## The asymmetry this makes visible

    Each leg's deductible proportion is FIXED by its trace (repayments reduce a
    blended borrowing pro rata; a taxpayer cannot pay down the personal half
    first). So the leg's deductible BALANCE is that fixed proportion of a
    balance that moves on its own terms:

      - the ADVANCE's balance amortizes, so its deductible balance -- and the
        deduction it yields -- ERODES year over year (#849's erosion);
      - the drawn LINE is interest-only and capitalizes into the charge, so its
        deductible balance does NOT erode.

    That is the trade-off, priced. Both are surfaced on ``YearResult`` as
    ``advance_deductible_balance`` / ``margin_deductible_balance``.

    ## The QC cap is shared, because the taxpayer is one taxpayer

    Quebec limits investment expenses to investment income, with an indefinite
    carry-forward (TA s.336.0.1). That is ONE limit over ONE taxpayer's whole
    investment position, so all three legs pool into a single
    ``qc_available``/``qc_carry_forward`` -- splitting them into per-leg pools
    would let one household deduct against the same income twice. The savings
    are then split back across the legs pro rata for REPORTING only
    (``readvance_tax_savings`` stays the SM line's own share, so #850 does not
    silently redefine a number other tables already print).
    """
    from simulation_state import compute_heloc_deductible_proportion
    yield_rate = ctx.config.non_reg_yield_rate

    # ── Leg 1: the SM readvance line (gate unchanged from before #850) ──
    sm_active = ctx.use_readvanceable and ws.new_sm_heloc > 0
    readvance_interest = ws.new_sm_heloc * ctx.heloc_rate if sm_active else 0.0
    sm_proportion = compute_heloc_deductible_proportion(
        ws.new_tracing, yield_rate=yield_rate
    ) if sm_active else 0.0
    sm_deductible = readvance_interest * sm_proportion

    # ── Leg 2: the mortgage advance (#850). Its deductible balance amortizes.
    # Issue #1075: when the mortgage carries a declared DEDUCTIBLE tranche
    # (the 3-tranche readvanceable structure's investment mortgage, mapped to
    # ``deductible_mortgage_balance``/``deductible_mortgage_interest`` -- the
    # exact sum of each flagged tranche's balance x ITS OWN rate), the leg is
    # priced at that EXACT interest, not at the blended-rate product the
    # traced proportion would produce. The blended product is only exact when
    # every tranche shares one rate: with a house tranche at 4.5% beside an
    # investment tranche at 6.5%, balance x weighted-rate x proportion
    # understates the s.20(1)(c) deduction the taxpayer can actually claim.
    # The flagged tranches were borrowed for investment, so their interest is
    # deductible in full -- the purpose trace of a NEW year-0 lump sum does
    # not apply to them (and would dilute them with the house tranche's
    # personal purpose). The exact year-0 interest is scaled by the same
    # amortization the traced path applies (the tranches amortize pro rata on
    # the single schedule), so the erosion #849 measures is preserved. A
    # config WITHOUT the keys keeps the pre-#1075 traced path byte-for-byte
    # (the golden household included -- DP#32).
    advance_proportion = compute_heloc_deductible_proportion(
        ws.new_advance_tracing, yield_rate=yield_rate)
    exact_deductible_interest = ctx.config.deductible_mortgage_interest
    exact_deductible_balance = ctx.config.deductible_mortgage_balance
    if exact_deductible_interest > 0 and ctx.config.mortgage_balance > 0:
        amort_scale = ws.opening_mortgage_balance / ctx.config.mortgage_balance
        ws.advance_deductible_interest = exact_deductible_interest * amort_scale
        ws.advance_deductible_balance = exact_deductible_balance * amort_scale
    else:
        ws.advance_deductible_interest = ws.mort.get('total_interest', 0.0) * advance_proportion
        ws.advance_deductible_balance = ws.new_mortgage_balance * advance_proportion

    # ── Leg 3: the drawn revolving margin (#850). Its deductible balance does
    # not amortize -- apply_margin_heloc_interest capitalizes it, never
    # repays it.
    # Issue #1036 D4/N2: deduct only interest that was PAID (serviced from pots)
    # or PAYABLE (capitalized into the balance) -- NOT the `heloc_interest_
    # unfunded` portion that was neither (it evaporated because the pots could
    # not service it and the charge had no room to capitalize it). A s.20(1)(c)
    # deduction requires interest paid or payable; the unfunded is neither, so
    # deducting it was a confident wrong number in the tax computation (and the
    # dollar-exact engine of the D2 inverted incentive). This runs AFTER
    # heloc_interest_servicing (see the rule order), so ws.heloc_interest_unfunded
    # is final here. 0.0 when there is no drawn margin or it is fully paid /
    # capitalized (the byte-identical path, incl. the golden household).
    margin_proportion = compute_heloc_deductible_proportion(
        ws.new_margin_tracing, yield_rate=yield_rate)
    paid_or_payable_margin_interest = ws.margin_heloc_interest - ws.heloc_interest_unfunded
    ws.margin_deductible_interest = paid_or_payable_margin_interest * margin_proportion
    ws.margin_deductible_balance = ws.new_heloc_balance * margin_proportion

    traced_deductible = ws.advance_deductible_interest + ws.margin_deductible_interest
    total_deductible = sm_deductible + traced_deductible

    if not sm_active and traced_deductible <= 0:
        # Nothing traced anywhere: no deduction, and the carry-forward simply
        # carries. Byte-for-byte the pre-#850 `else` branch -- the golden
        # household lands here every year of its 46-year horizon.
        ws.new_qc_carry_forward = ws.opening_qc_carry_forward
        ws.traced_borrowing_tax_savings = 0.0
        return False

    # ── The shared QC investment-expense cap ──
    # The income base is the investment income of the pots the traced
    # borrowings actually bought: the SM leg's proceeds are `new_sm_investment`
    # (its own pot), while the advance's and the drawn line's proceeds were
    # allocated by `fill_room` into the plain non-registered account. The
    # non-reg pot is therefore added ONLY when one of those two legs is present
    # -- an SM-only household's cap must not silently widen because #850
    # landed. Whether QC's pool should ALWAYS have counted the plain non-reg
    # account's income is a real question, and a separate one from this issue.
    #
    # ── The shared QC investment-expense cap (QUEBEC households only) ──
    # The income base is the SCHEDULE L net investment income of the pots the
    # traced borrowings actually bought (#1035): eligible + non-eligible
    # dividends, interest/other, and HALF the realized capital-gain component
    # of the declared yield (Schedule L line 5) -- NOT a flat
    # ``balance * yield_rate`` product, which could not tell a dividend
    # portfolio from a growth one and permanently under-used the cap for
    # growth-tilted households. The SM leg's proceeds are `new_sm_investment`
    # (its own pot), while the advance's and the drawn line's proceeds were
    # allocated by `fill_room` into the plain non-registered account. The
    # non-reg pot is therefore added ONLY when one of those two legs is present
    # -- an SM-only household's cap must not silently widen because #850
    # landed. Whether QC's pool should ALWAYS have counted the plain non-reg
    # account's income is a real question, and a separate one from this issue.
    #
    # The cap itself is QUEBEC-ONLY (TA s.336.0.1): a household whose tax
    # province is not Quebec faces no investment-income limitation in any
    # statute, so it deducts the full traced interest provincially too, and
    # any carry-forward it somehow carries simply carries (#1035).
    qc_income_pots = [ws.new_sm_investment]
    if traced_deductible > 0:
        qc_income_pots.append(ws.new_nonreg_bal)
    from countries.canada.provinces.quebec.quebec_deduction import (
        cap_qc_investment_interest)
    province = '' if ctx.config is None else str(ctx.config.province)
    if province.strip().lower() in ('qc', 'quebec', 'québec'):
        qc_deductible, new_qc_carry_forward = cap_qc_investment_interest(
            total_deductible=total_deductible,
            investment_income=_sm_schedule_l_income(ctx, qc_income_pots),
            opening_carry_forward=ws.opening_qc_carry_forward)
    else:
        qc_deductible = total_deductible
        new_qc_carry_forward = ws.opening_qc_carry_forward

    # Issue #1033: the deduction is routed through taxable income by TWO
    # mechanisms, gated so exactly ONE fires per phase (Blocker 1: the
    # pre-fix code let both fire and paid the same dollar twice):
    #
    #   * STATUTORY side-credit (accumulation only): the deduction's federal+
    #     QC statutory tax saving, valued at bracket-fill via
    #     ``tax_calculator.deduction_value`` -- the SAME ``tax_on_income`` /
    #     year-brackets path the prologue's ``_income_tax_by_adult`` uses for
    #     rental and private-loan s.20(1)(c) interest (DP#9). A deduction
    #     crossing a bracket boundary is worth less than the flat top marginal
    #     rate (the pre-#1033 code used ``amount * primary_marginal_rate``).
    #     Real improvement: a zero-income retiree used to get a bogus
    #     ``qc_deductible * marginal_rate(0)`` = 25.69% side-credit;
    #     ``deduction_value(0, ...)`` now returns 0.
    #   * OAS-CLAWBACK routing (retirement only, in ``apply_retirement_
    #     drawdown``): the FEDERAL ``total_deductible`` (uncapped -- the QC cap
    #     is a provincial limit, TA s.336.0.1, separate) reduces the clawback
    #     base, buying OAS recovery-tax relief + lowering the draw's
    #     progressive base.
    #
    # The gate is ``ctx.primary_retired`` (the deduction is the PRIMARY's;
    # Canada has no joint filing). In retirement ``primary_taxable_income`` is
    # NOT zero -- rental operating income and private-loan interest income
    # survive (``primary_income`` is zeroed at retirement, those are not) --
    # so the side-credit would double-count against the routing. Zeroing it in
    # retirement leaves the routing as the sole mechanism. The cost: the
    # statutory saving on the NON-drawdown income slice (rental/loan) in
    # retirement is not captured (the routing reaches only the cpp+pension+draw
    # base) -- an edge case (retiree with rental/loan income AND a leveraged
    # margin), documented as a known limit, not double-counted.
    #
    # The side-credit is valued as a FEDERAL + QUEBEC pair (#1035): the federal
    # slice on ``total_deductible`` (uncapped -- no federal limit), the Quebec
    # slice on ``qc_deductible``, each on its own bracket set. Pre-#1035 the
    # single capped amount was valued at the blended COMBINED rate, which
    # suppressed the federal deduction whenever the cap bound. That conflation
    # is NOT the same as Major 3's routing leak (routing ``qc_deductible`` into
    # the FEDERAL clawback base), which #1033 fixed by routing
    # ``total_deductible``.
    # Direct unit-test callers that pass ``primary_marginal_rate`` but not
    # ``year_brackets`` keep the pre-#1033 flat-rate valuation byte-for-byte
    # (the live fold always passes ``year_brackets``).
    from tax_calculator import deduction_value
    if ctx.year_brackets is not None:
        fed_brackets, prov_brackets = _year_split_brackets_for(ctx)
        if fed_brackets is not None:
            # Issue #1035: the FEDERAL s.20(1)(c) deduction has no
            # investment-income limit, so its slice is valued on the UNCAPPED
            # ``total_deductible``; only the QUEBEC slice is valued on the
            # capped ``qc_deductible``. One blended combined-rate valuation of
            # the capped amount (the pre-#1035 conflation) suppressed the
            # federal deduction whenever the cap bound.
            tax_savings = (
                deduction_value(
                    ctx.primary_taxable_income, total_deductible, fed_brackets)
                + deduction_value(
                    ctx.primary_taxable_income, qc_deductible, prov_brackets))
        else:
            # Split unavailable for this year (no tax data): value the capped
            # amount at the combined brackets -- the pre-#1035 spelling --
            # rather than fabricate a split (DP#32).
            tax_savings = deduction_value(
                ctx.primary_taxable_income, qc_deductible, ctx.year_brackets)
    else:
        tax_savings = qc_deductible * ctx.primary_marginal_rate
    if ctx.primary_retired:
        # Blocker 1: the routing in apply_retirement_drawdown handles the
        # deduction in retirement; the side-credit would double-count.
        tax_savings = 0.0
    # Report-only split of ONE pooled deduction. When nothing was traced this
    # year the whole (carry-forward-funded) saving stays on the SM leg, exactly
    # where it was reported before #850.
    sm_share = sm_deductible / total_deductible if total_deductible > 0 else 1.0

    ws.readvance_interest = readvance_interest
    ws.deductible_proportion = sm_proportion
    ws.qc_deductible = qc_deductible
    ws.new_qc_carry_forward = new_qc_carry_forward
    # Major 3: route the FEDERAL s.20(1)(c) deduction (``total_deductible``, no
    # investment-income limit) through the OAS clawback base -- NOT the
    # Quebec-capped ``qc_deductible``. The OAS recovery tax is federal; leaking
    # the QC cap into it silently dropped ~61% of the relief in the motivating
    # fixture. ``qc_deductible`` stays the provincial side-credit amount (#1035).
    ws.sm_interest_deduction = total_deductible
    ws.readvance_tax_savings = tax_savings * sm_share
    ws.traced_borrowing_tax_savings = tax_savings * (1.0 - sm_share)
    return True

@rule('sm_investment_growth')
def apply_sm_investment_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow the SM (readvanced) investment at the same after-tax rate as
    the plain non-reg account (#576: it is non-registered/taxable by
    construction). Depends on ``non_reg_growth`` (the rate) and
    ``sm_readvance`` (the balance).
    """
    if ctx.use_readvanceable:
        pre = ws.new_sm_investment
        ws.new_sm_investment *= (1 + ws.non_reg_growth_rate)
        return pre > 0 and ws.non_reg_growth_rate != 0
    return False

@rule('margin_heloc_interest')
def apply_margin_heloc_interest(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Interest on the drawn HELOC margin -- capitalized only as far as the
    registered charge has room, serviced in cash beyond that (issue #681).
    Depends on ``mortgage`` (new_mortgage_balance, the other claim on the same
    charge) and ``non_reg_growth`` (the pot the serviced interest is paid
    from). Issue #577: an undrawn margin has a $0 balance here, so no interest
    arises at all.

    Issue #681: this rule used to capitalize interest UNCONDITIONALLY --
    ``new_heloc_balance = opening * (1 + rate)``, forever, with no ceiling. A
    household that drew $400,000 of margin at year 0 compounded it to over
    $1,000,000 of revolving debt by the end of the horizon, straight through
    the $640,000 charge, and the run stayed green. Bounding only the readvance
    (the other half of #681) would have left this channel wide open: the debt
    would have breached the charge anyway, just more slowly.

    A revolving facility cannot capitalize interest past its limit. That is
    not a modelling nicety, it is what the product IS: the lender requires at
    minimum an interest-only payment, and the balance cannot exceed the
    authorized limit. So interest capitalizes only into the room the charge
    actually leaves; the rest must be **serviced in cash** -- booked by the
    ``heloc_interest_servicing`` rule below, which runs once the SM balances
    it is paid out of are final.

    NOTE (issue #1036): the contract declares
    ``liabilities[kind=heloc].capitalize_interest`` and the engine now READS
    it (mapped to ``property.capitalize_interest`` -> ``SimulationConfig
    .capitalize_interest``). When False, the drawn-margin interest is serviced
    in cash -- none of it capitalizes, regardless of charge room. When True
    (the default when the contract omits the field, and the pre-#1036
    behaviour), capitalize up to the charge and service the rest. The
    capitalized-vs-serviced split is honoured below. This closes the
    declaration gap this NOTE used to record.
    """
    margin_heloc_interest = ws.opening_heloc_balance * ctx.heloc_rate

    # Room left under the shared charge, priced against the OTHER claims on
    # it: the amortizing mortgage, plus the already-drawn revolving balances
    # (this margin, and any SM-readvanced line carried in from last year).
    room = charge_room_for_readvance(
        # Issue #963 (epic #956 bite F): the charge is registered against the
        # principal's APPRECIATED value this year, not the static `house_value`
        # (mirrors the SM readvance rule above). Absence-safe: returns the
        # static `house_value` when no rate is declared, byte-identical today.
        house_value=_principal_value_for_year(ctx.config, ctx.calendar_year),
        mortgage_balance=ws.new_mortgage_balance,
        drawn_revolving=ws.opening_heloc_balance + ws.opening_readvance_heloc_balance,
        charge_ltv_limit=ctx.config.charge_ltv_limit,
        heloc_ltv_limit=ctx.config.heloc_ltv_limit,
    )
    capitalized = min(margin_heloc_interest, room)
    # Issue #1036: honour liabilities[kind=heloc].capitalize_interest. When
    # False, the household pays the drawn-margin interest in CASH -- none of
    # it capitalizes into the balance, regardless of how much charge room is
    # left (a retiree servicing their HELOC interest in cash is no longer
    # modelled as capitalizing it up to the charge anyway). When True (the
    # default when the contract omits the field, and the pre-#1036 behaviour),
    # capitalize up to the charge and service the rest in cash. The serviced
    # portion is booked by the ``heloc_interest_servicing`` rule below, which
    # runs once the SM balances it is paid out of are final -- so wiring this
    # here is money-conserving (the interest leaves the balance sheet via the
    # pots, never silently absorbed) and DP#32-honest (when the pots cannot
    # cover it, ``heloc_interest_unfunded`` reports it).
    if not ctx.config.capitalize_interest:
        capitalized = 0.0

    ws.margin_heloc_interest = margin_heloc_interest
    ws.margin_heloc_interest_capitalized = capitalized
    ws.margin_heloc_interest_serviced = margin_heloc_interest - capitalized
    ws.new_heloc_balance = ws.opening_heloc_balance + capitalized
    return ws.opening_heloc_balance > 0

@rule('heloc_interest_servicing')
def apply_heloc_interest_servicing(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Pay, in cash, the HELOC interest the charge had no room to capitalize
    (issue #681). Depends on ``margin_heloc_interest`` (the serviced amount)
    and on ``non_reg_growth`` / ``sm_investment_growth`` (the pots it is paid
    from, which must be final before it draws on them).

    The cash comes out of the household's liquid non-registered savings
    first, then out of the SM investment itself -- selling units to pay the
    interest on the loan that bought them, which is exactly the position an
    over-levered Smith-Manoeuvre household is in.

    That servicing cost is REAL money leaving the balance sheet, and booking
    it is a large part of what stops "borrow the maximum" from looking free:
    before #681 the interest simply inflated the debt, forever, past a charge
    no lender would have granted. What cannot be funded from either pot is
    recorded on ``heloc_interest_unfunded`` -- a household that cannot pay
    the interest on its own facility, reported rather than silently absorbed
    (DP#32).

    Issue #1034: selling either pot to service interest is a real DISPOSITION
    -- it realizes a capital gain (taxed) and reduces the cost basis
    PROPORTIONALLY to the units sold (not clamped to the remaining FMV). Both
    legs (non-reg first, then SM) reuse ``price_sm_unwind`` -- the one spelling
    of the sell-to-raise-net-after-tax arithmetic (DP#9) -- with
    ``sm_heloc=0`` because this sale SERVICES INTEREST (a cash outflow), it
    does not repay HELOC principal. The sale is GROSSED UP so the after-tax
    proceeds cover the interest (the tax is funded from the proceeds, so the
    pot shrinks by MORE than the cash delivered). The gain's taxable slice
    stacks on the year's taxable income for the year-end AMT comparison
    (apply_amt reads ``heloc_servicing_realized_gain`` / ``heloc_servicing_taxable``);
    the tax itself is paid once here (via the gross-up), never re-charged.
    """
    unfunded = ws.margin_heloc_interest_serviced
    if unfunded <= 0:
        ws.heloc_interest_unfunded = 0.0
        ws.heloc_servicing_funded = 0.0
        return False

    # The household's already-recognized taxable income this year: employment
    # (accumulation) + CPP/pension + the drawdown's taxable slice (retirement).
    # This is the SAME base sm_unwind stacks on (``drawdown_other_taxable_income
    # + drawdown_taxable``), extended with employment income for the
    # accumulation phase where this rule fires (sm_unwind runs in retirement,
    # where employment is zero and the two expressions coincide). One spelling
    # of the stacking base for both legs.
    other_income = (ctx.primary_taxable_income + ctx.spouse_taxable_income
                    + ws.drawdown_other_taxable_income + ws.drawdown_taxable)

    def _service_from_pot(fmv_before: float, acb_before: float,
                          net_need: float):
        """Sell ``net_need`` cash out of one capital pot, realizing and taxing
        the gain, reducing ACB proportionally. Returns
        ``(new_fmv, new_acb, realized_gain, tax, net_delivered)``.

        Reuses ``price_sm_unwind`` (DP#9 -- one spelling of the disposition
        arithmetic). Raises if ``ctx.year_brackets`` is None -- a capital gain
        is being priced and a silent 0% flat fallback (DP#32 fabricated zero)
        is refused, mirroring ``property_disposition``'s guard."""
        # Caller invariant: ``net_need > 0`` and ``fmv_before > 0`` (each leg
        # guards ``if from_X > 0.0`` before calling), so price_sm_unwind is
        # reached with a real pot to sell.
        # D6: refuse a silent 0% flat fallback (DP#32 fabricated zero) when
        # ``ctx.year_brackets`` is None, mirroring ``property_disposition``'s
        # guard. Gated on ``fmv_before > acb_before`` because a no-gain
        # disposition (ACB == FMV, e.g. a freshly-funded pot) has nothing to
        # under-tax -- the tax is legitimately 0 there.
        if ctx.year_brackets is None and fmv_before > acb_before:
            raise ValueError(
                "heloc_interest_servicing rule needs ctx.year_brackets to "
                "price the capital gain on a forced disposition; got None (the "
                "prologue resolves these for the marginal rates -- a direct "
                "caller must pass them too).")
        from countries.canada.retirement_transition import price_sm_unwind
        unwind = price_sm_unwind(
            net_need=net_need,
            sm_fmv=fmv_before,
            sm_acb=acb_before,
            sm_heloc=0.0,
            brackets=ctx.year_brackets,
            other_income=other_income,
            inclusion_rate=ctx.config.capital_gains_inclusion,
        )
        f = (unwind.gross_sold / fmv_before if fmv_before > 0 else 0.0)
        new_fmv = max(0.0, fmv_before - unwind.gross_sold)
        new_acb = max(0.0, acb_before - f * acb_before)
        return (new_fmv, new_acb, unwind.realized_gain, unwind.tax,
                unwind.net_delivered)

    inclusion = ctx.config.capital_gains_inclusion

    # Non-reg leg (drains FIRST). Pre-#1034 this clamped ACB (``min(acb, fmv)``)
    # and recognized no gain -- the identical bug the SM leg had. Fixed the
    # same way: realize and tax the gain, reduce ACB proportionally.
    from_nonreg = min(unfunded, max(0.0, ws.new_nonreg_bal))
    if from_nonreg > 0.0:
        ws.new_nonreg_bal, ws.new_nonreg_acb, gain, tax, delivered = (
            _service_from_pot(ws.new_nonreg_bal, ws.new_nonreg_acb, from_nonreg))
        unfunded -= delivered
        ws.heloc_servicing_funded += delivered
        ws.heloc_servicing_realized_gain += gain
        ws.heloc_servicing_tax += tax
        ws.heloc_servicing_taxable += gain * inclusion

    # SM leg (drains SECOND).
    from_sm = min(unfunded, max(0.0, ws.new_sm_investment))
    if from_sm > 0.0:
        ws.new_sm_investment, ws.new_sm_cost_basis, gain, tax, delivered = (
            _service_from_pot(ws.new_sm_investment, ws.new_sm_cost_basis,
                              from_sm))
        unfunded -= delivered
        ws.heloc_servicing_funded += delivered
        ws.heloc_servicing_realized_gain += gain
        ws.heloc_servicing_tax += tax
        ws.heloc_servicing_taxable += gain * inclusion

    ws.heloc_interest_unfunded = max(0.0, unfunded)
    return True

@rule('rrsp_refund_heloc_paydown')
def apply_rrsp_refund_heloc_paydown(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Apply this year's RRSP tax refund to pay down the personal-draw
    HELOC margin. Depends on ``rrsp_deduction`` (the refund amount) and
    ``margin_heloc_interest`` (the balance to pay down).
    """
    if not ctx.deduct_later:
        rrsp_refund = (ws.p_rrsp_actual + ws.s_rrsp_actual) * ctx.primary_marginal_rate + ws.sp_rrsp_actual * ctx.spouse_marginal_rate
    else:
        rrsp_refund = ws.rrsp_deduction_savings + ws.spouse_deduction_savings
    ws.rrsp_refund = rrsp_refund

    heloc_paydown = 0.0
    # Issue #1040: a borrow_to_invest option declared hold_draw=true opts its
    # draw OUT of this sweep (SimulationConfig.hold_borrow_to_invest_draw,
    # set per exploration cell by optimize.run_borrow_to_invest_exploration).
    # The drawn balance is NOT reduced by the refund -- the refund stays in
    # the household's cash and flows to the usual allocation instead -- while
    # apply_margin_heloc_interest still prices/capitalizes (or cash-services,
    # per capitalize_interest) the interest exactly as before. Default False
    # = the pre-#1040 debt-sweep behaviour, byte-identical (DP#32: absence is
    # the fallback, never a coercion).
    if (rrsp_refund > 0 and ws.new_heloc_balance > 0
            and not ctx.config.hold_borrow_to_invest_draw):
        heloc_paydown = min(rrsp_refund, ws.new_heloc_balance)
        ws.new_heloc_balance -= heloc_paydown
    ws.heloc_paydown = heloc_paydown
    ws.new_heloc_rrsp_paydown = ws.opening_heloc_rrsp_paydown + heloc_paydown
    return heloc_paydown > 0

@rule('sm_unwind')
def apply_sm_unwind(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #1017: under ``liquidate_to_target``, UNWIND the Smith-Manoeuvre
    sleeve to fund the spending shortfall the ordinary financial drawdown
    (``retirement_drawdown``) and the forced RRIF minimum (``rrif_minimum``)
    could not cover.

    The SM sleeve is a leveraged non-reg portfolio that compounds untouched by
    the ordinary drawdown (it is not a ``_DRAWDOWN_SOURCES`` token), so on a
    leveraged household the die-with-zero mode delivered $0/yr for ~15 years
    while a $5M SM portfolio compounded and a $520k HELOC rode to death. This
    rule closes the leverage: sell a slice of the sleeve, realize the capital
    gain (taxed through the progressive brackets), repay the SM HELOC
    proportionally from the proceeds, and deliver the NET to the spending
    target. Ordered AFTER ``rrif_minimum`` (so the shortfall reflects the true
    gap once every financial account + the forced minimum have fired) and
    BEFORE ``solvency`` (so the net it delivers counts in the cash-flow
    identity and the waterfall does not force-liquidate for a gap the unwind
    already filled).

    Gated by ``ws.liquidate_to_target`` (the opt-in die-with-zero mode): absent
    or False -> strict no-op -> byte-identical (DP#32). A household with no SM
    sleeve (``new_sm_investment`` == 0) -> nothing to unwind -> no-op. The SM
    gain is priced stacking on the household's already-recognized taxable
    income (``drawdown_other_taxable_income + drawdown_taxable`` -- the
    discretionary draw + forced RRIF + CPP/pension); the OAS clawback the SM
    gain could trigger is NOT modelled here (a noted approximation -- the
    deep-retirement unwind typically lands above the clawback threshold
    already, and the discretionary drawdown's clawback machinery is the one
    spelling of that tax, not duplicated here, DP#9).

    Money conservation (DP#18): the SM balance drops by ``gross_sold``, the
    HELOC drops by ``heloc_repaid``, the tax leaves the household, and
    ``net_delivered`` (``gross_sold - tax - heloc_repaid``) flows to spending
    via ``drawdown_net_delivered`` -- so the balance sheet's net assets drop by
    exactly ``tax + net_delivered`` (the HELOC repayment is an asset-for-debt
    swap within the balance sheet, net-zero on net assets). The proportional
    HELOC repayment couples the asset and its financing so both drain to zero
    together as the sleeve unwinds (terminal SM == 0 -> terminal HELOC == 0).
    """
    if not ws.liquidate_to_target:
        return False
    if ws.new_sm_investment <= 0.0:
        return False
    shortfall = ws.drawdown_net_target - ws.drawdown_net_delivered
    if shortfall <= 1.0:
        return False

    from countries.canada.retirement_transition import price_sm_unwind
    # The household's already-recognized taxable income this year: the
    # discretionary drawdown's taxable slice + the forced RRIF minimum +
    # CPP/pension (all household totals). The SM capital gain stacks on top so
    # it is priced at the marginal band it lands in.
    other_income = ws.drawdown_other_taxable_income + ws.drawdown_taxable
    unwind = price_sm_unwind(
        net_need=shortfall,
        sm_fmv=ws.new_sm_investment,
        sm_acb=ws.new_sm_cost_basis,
        sm_heloc=ws.new_sm_heloc,
        brackets=ctx.year_brackets,
        other_income=other_income,
        inclusion_rate=ctx.config.capital_gains_inclusion,
        flat_rate=ws.retiree_marginal_rate,
    )
    # price_sm_unwind always sells a positive gross when called (sm_fmv > 0
    # and net_need > 0 -- the rule's early returns guarantee both), so
    # gross_sold > 0 here by construction; no defensive zero guard (a dead
    # branch would be exactly the uncovered code DP#32 frowns on).

    # Apply the unwind to the SM sleeve + HELOC. The cost basis retires
    # proportionally to the FMV sold (selling fraction f = gross_sold / fmv;
    # the ACB sold is f * acb), so the remaining sleeve's gain fraction is
    # unchanged -- the unwind does not silently crystallize a different gain
    # ratio than the deemed disposition would (DP#19). Floored at zero for
    # floating-point hygiene (a -1e-15 sleeve would be a silent over-sale,
    # not a value) via ``max`` rather than a guard so there is no uncovered
    # dead branch (DP#32).
    f = unwind.gross_sold / ws.new_sm_investment if ws.new_sm_investment > 0 else 0.0
    ws.new_sm_investment = max(0.0, ws.new_sm_investment - unwind.gross_sold)
    ws.new_sm_cost_basis = max(0.0, ws.new_sm_cost_basis - f * ws.new_sm_cost_basis)
    ws.new_sm_heloc = max(0.0, ws.new_sm_heloc - unwind.heloc_repaid)

    # Deliver the net to the spending target via the drawdown channels, so the
    # solvency cash-flow identity (``available`` adds ``drawdown_net_delivered``)
    # sees the unwind's funding and the shortfall #707 surfaces is the TRUE
    # post-unwind gap. The gross proceeds + realized gain are surfaced for the
    # year-end AMT base (#754) and transparency (DP#32).
    ws.drawdown_net_delivered += unwind.net_delivered
    ws.drawdown_total += unwind.gross_sold
    # The taxable slice of the SM disposition is the realized capital gain at
    # the capital-gains inclusion rate (mirroring the non-reg drawdown's
    # ``taxable_withdrawn`` = cg_inclusion x realized gain).
    ws.drawdown_taxable += unwind.realized_gain * ctx.config.capital_gains_inclusion
    ws.drawdown_realized_capital_gain += unwind.realized_gain
    ws.sm_unwind_proceeds = unwind.gross_sold
    ws.sm_unwind_tax = unwind.tax
    ws.sm_unwind_heloc_repaid = unwind.heloc_repaid
    ws.sm_unwind_net_delivered = unwind.net_delivered
    ws.sm_unwind_realized_gain = unwind.realized_gain
    return True
