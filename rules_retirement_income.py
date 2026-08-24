"""The ``retirement_income`` rule: the per-year retirement transition.

CPP/OAS/pension onset, the employment-income stop, and the NET drawdown-target
sizing every decumulation rule downstream prices against. First in
``RULE_ORDER`` -- it depends only on member data + pre-retirement income +
the year's brackets, and every consumer runs later.

Split out of ``simulation_rules.py``; the rule body is unchanged.
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule


@rule('retirement_income')
def apply_retirement_income(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Epic #795 bite 1 (DP#10/#26): the per-year retirement transition.

    Once a member reaches ``retirement_age`` (a date-computed eligibility
    gate, DP#1/#28), stop their employment income and turn on CPP/OAS/pension
    + size the NET drawdown target. This used to be computed inline in the
    fold's prologue (``simulation._retirement_transition_for``), spelled
    twice -- once in ``simulate_year`` and once in ``_run_monthly`` (DP#9).
    It is now a single registered rule; the prologue passes only the
    pre-retirement GROWN incomes + the resolved retirement status + the
    year-brackets + the tax indexation rate (``RuleContext``), and this rule
    writes the government-income / drawdown-param outputs to
    ``YearWorkingState``, which ``retirement_drawdown`` / ``rrif_minimum`` /
    ``solvency`` read.

    DP#25: ``countries.canada`` is imported lazily inside the body (this
    module keeps no jurisdiction import at top level); ``tax_calculator``
    primitives (``marginal_rate`` / ``tax_on_income`` / ``bracket_ceiling``)
    are jurisdiction-agnostic and imported lazily too. The body is the
    verbatim computation ``simulation._retirement_transition_for`` performed
    -- moved, not changed -- so every number is preserved byte-for-byte.

    Returns True when any member is retired this year (the transition had an
    observable effect), False for a pure pre-retirement year (no-op, all
    outputs stay at their seeded 0.0/False/None defaults).
    """
    from countries.canada.retirement_transition import (
        member_retirement_income,
        DEFAULT_NET_REPLACEMENT_RATE, retirement_spending_target,
    )
    from countries.canada.retirement import (
        get_oas_annual_max, get_oas_clawback_threshold, _get_rrif_rates,
    )
    from tax_calculator import marginal_rate, tax_on_income, bracket_ceiling

    config = ctx.config
    members = config.family_members
    primary = next((m for m in members if m.get('role') == 'primary'), {})
    spouse = next((m for m in members if m.get('role') == 'spouse'), {})

    sim_year = ctx.calendar_year
    start_year = ctx.calendar_year - ctx.year

    p_retired = ctx.primary_retired
    s_retired = ctx.spouse_retired
    any_retired = p_retired or s_retired
    if not any_retired:
        return False

    primary_income = ctx.primary_income_pre
    spouse_income = ctx.spouse_income_pre

    # Year-versioned OAS figures, with explicit input.retirement overrides.
    # Issue #592: `0` is a value ("this household gets no OAS"), not an
    # absent key -- `or` treats it as falsy and silently reinstates the
    # code table. Use `is None`: absent/None -> year-versioned table;
    # any configured number (including 0) -> that number.
    #
    # A non-zero override is a scalar, and the fold runs one sim_year per
    # call -- a bare override would freeze OAS at one number across the
    # whole horizon, defeating the year-versioning CPP_OAS_BY_YEAR exists
    # to provide. Per DP#20, treat the override as the amount *as of
    # start_year* and index it forward at the same inflation assumption the
    # tax-data year-projection uses, so an override participates in
    # indexation instead of silently disabling it.
    ret = config.retirement_data or {}
    years_ahead = sim_year - start_year
    indexation_factor = (1 + ctx.tax_indexation_rate) ** years_ahead

    oas_max_override = ret.get('oas_annual_max')
    if oas_max_override is None:
        oas_max = get_oas_annual_max(sim_year)
    else:
        oas_max = oas_max_override * indexation_factor

    oas_threshold_override = ret.get('oas_clawback_threshold')
    if oas_threshold_override is None:
        oas_threshold = get_oas_clawback_threshold(sim_year)
    else:
        oas_threshold = oas_threshold_override * indexation_factor

    drawdown_order = ret.get('drawdown_order')
    if drawdown_order is None:
        drawdown_order = ['tfsa', 'non_reg', 'rrsp']
    # Issue #301: size drawdown to a NET spending target, not gross income.
    net_replacement_rate = ret.get('net_replacement_rate', DEFAULT_NET_REPLACEMENT_RATE)
    spending_target = ret.get('spending_target')
    # Issue #1009: opt-in die-with-(near)-zero drawdown mode. A bool leaf --
    # absent and an explicit False both mean "off" (a boolean has no
    # "zero is a value" concern, DP#32), so a plain truthiness test is
    # correct here. Read once and threaded to apply_retirement_drawdown via
    # ws.liquidate_to_target; absent => the configured order runs unchanged.
    liquidate_to_target = bool(ret.get('liquidate_to_target', False))

    # Stop employment income for retired members (no salary, no growth).
    new_primary = 0.0 if p_retired else primary_income
    new_spouse = 0.0 if s_retired else spouse_income

    # Issue #363 PR 4: keep the PER-MEMBER government income, not just the
    # household sums. A retired member's cpp/oas/pension seeds THAT spouse's own
    # bracket stack in the per-spouse drawdown split below; a non-retired member
    # contributes nothing (member_retirement_income returns zeros).
    from countries.canada.retirement_transition import MemberRetirementIncome
    ri_p = (member_retirement_income(primary, sim_year, oas_max, oas_threshold)
            if (primary and p_retired) else MemberRetirementIncome())
    ri_s = (member_retirement_income(spouse, sim_year, oas_max, oas_threshold)
            if (spouse and s_retired) else MemberRetirementIncome())
    # Issues #711/#712: CPP/QPP sharing + pension income splitting, as ELECTIONS
    # the optimizer sweeps (DP#22/#30), applied here as PURE income transfers
    # between the two retired spouses BEFORE the per-spouse bases below are
    # built. Both conserve the household total, so covered_net / net_shortfall /
    # the drawdown net target are byte-unchanged (money conservation) — only the
    # per-spouse SPLIT moves, so each spouse's own progressive-drawdown stack and
    # OAS-clawback base shift, and the household's retirement tax with them. The
    # per-spouse marginal rates the drawdown prices against (this rule's
    # draw_rate_primary/spouse, #363 PR 4) are the same rates that decide the
    # split direction — no parallel tax model is built. Both are no-ops unless
    # BOTH spouses are retired (there is no spouse to share/split with otherwise)
    # AND the election is present; absent the election they default to 0 and the
    # transfer vanishes, preserving current behaviour exactly (DP#13/#32).
    both_retired = bool(primary and spouse and p_retired and s_retired)
    cpp_share = ret.get('cpp_share', 0.0)
    pension_split_pct = ret.get('pension_split_pct', 0.0)
    if both_retired and cpp_share:
        from countries.canada.cpp_sharing import share_cpp_amounts
        ri_p.cpp, ri_s.cpp = share_cpp_amounts(ri_p.cpp, ri_s.cpp, cpp_share)
    if both_retired and pension_split_pct:
        from countries.canada.pension_split_optimizer import split_pension_amounts
        # Split from the higher-bracket spouse to the lower one; the per-spouse
        # taxable base (own CPP + pension) ranks them, and (brackets progressive)
        # a higher base is a higher marginal rate — the rate the drawdown prices.
        primary_is_higher = (ri_p.cpp + ri_p.pension) >= (ri_s.cpp + ri_s.pension)
        ri_p.pension, ri_s.pension = split_pension_amounts(
            ri_p.pension, ri_s.pension, pension_split_pct, primary_is_higher)

    cpp = ri_p.cpp + ri_s.cpp
    oas = ri_p.oas + ri_s.oas
    pension = ri_p.pension + ri_s.pension

    # Issue #1020 (S04 Step 1): GIS (Guaranteed Income Supplement) -- the one
    # government program whose computation lived in a helper
    # (``countries.canada.retirement.gis_benefit``) but was never WIRED into
    # the fold, so the optimizer was blind to it. Now computed here, reusing
    # the existing helper (DP#9 -- the GIS math is spelled once, in the
    # year-versioned pure function; this rule calls it).
    #
    # CRA's income test is PRIOR-YEAR: GIS for year N is determined by the
    # year N-1 countable income. That is what makes the pre-65 preservation
    # maneuver effective (low income in the year before 65 -> full GIS at
    # 65+) and it is what breaks the circularity that would otherwise arise
    # (GIS depends on the drawdown amount, and the drawdown shortfall --
    # which this rule sizes -- depends on GIS). The prior year's countable
    # base is the prior year's retirement income EXCLUDING OAS (OAS is
    # excluded from the GIS test by statute -- see ``gis_benefit``'s
    # ``net_income`` contract), threaded in from the prior ``YearResult`` by
    # the prologue. None (no prior year -- the first retirement year's prior
    # year is a WORKING year, or a direct unit-test caller) => GIS stays at
    # its seeded 0.0 (DP#32: absence is a loud no-op, never a silent zero-
    # coercion; a unit test that does not pass ``prior_gis_countable_income``
    # is byte-identical to before).
    gis = 0.0
    prior_countable = ctx.prior_gis_countable_income
    if prior_countable is not None:
        from countries.canada.retirement import gis_benefit
        # is_coupled: a spouse member exists (the household files as a couple
        # for GIS purposes). gis_benefit applies the coupled max + the $4k
        # employment-income exemption + the 50% reduction on the rest.
        is_coupled = bool(spouse)
        gis = gis_benefit(prior_countable, is_coupled=is_coupled,
                         year=sim_year)['gis_amount']
    ws.gis_income = gis

    year_brackets = ctx.year_brackets if ctx.year_brackets is not None else []

    # Drawdown need (issues #301/#363): size to the NET spending shortfall
    # after other retirement income; simulation_state.plan_drawdown_net then
    # fills that net target account-by-account, tax-exact per source (#579).
    # CPP/OAS/pension are netted off the target first.
    net_shortfall = 0.0
    draw_rate = 0.0
    # Issue #758: the EFFECTIVE retirement spending target (spending_target
    # or net_replacement_rate x pre-net), 0.0 in every pre-retirement year.
    # Passed to the #679 solvency rule so that in retirement it charges the
    # RETIREMENT spending figure (what the drawdown is sized to) -- not the
    # working-phase `living_costs` -- and does not double-count spending the
    # household is already funding via the drawdown.
    eff_retirement_spending_target = 0.0
    pre_gross = ctx.base_primary_income + ctx.base_spouse_income
    pre_net = pre_gross - (
        tax_on_income(ctx.base_primary_income, year_brackets)
        + tax_on_income(ctx.base_spouse_income, year_brackets)
    )
    target_net = retirement_spending_target(
        pre_net, net_replacement_rate, spending_target)
    eff_retirement_spending_target = target_net
    # Net income already covered by government benefits + any remaining
    # employment pay (CPP/OAS/pension are received net of further tax here;
    # remaining employment income is netted by its own marginal tax).
    # Issue #1020: GIS is non-taxable cash, so it covers spending
    # dollar-for-dollar and reduces the discretionary drawdown shortfall --
    # the whole point of the preservation maneuver (GIS replaces registered
    # drawdown that would have been taxed + clawed back GIS).
    covered_net = cpp + oas + pension + gis
    covered_net += (new_primary + new_spouse) - (
        tax_on_income(new_primary, year_brackets)
        + tax_on_income(new_spouse, year_brackets)
    )
    net_shortfall_pre_rrif = max(0.0, target_net - covered_net)
    # Issue #1001: the forced RRIF minimum's AFTER-TAX proceeds also cover
    # part of the net spending target (the mandatory withdrawal is cash the
    # household receives), so the discretionary drawdown must be sized to the
    # TRUE residual shortfall, not the full gap. The RRIF after-tax is priced
    # here (pure, no side effects — apply_rrif_minimum books the tax/clawback
    # exactly once, later) using the SAME per-spouse machinery
    # (price_forced_rrif_tax) and the per-spouse bases built below, then netted
    # into the shortfall. See the #1001 block after the per-spouse bases.
    net_shortfall = net_shortfall_pre_rrif
    # DEPRECATED flat-rate fallback (#579). Since #363 the drawdown re-brackets
    # the taxable draw progressively against the year-versioned `year_brackets`
    # (passed to plan_drawdown_net via ws.drawdown_other_taxable_income); this
    # scalar is only consulted when no bracket table is available. Kept so the
    # plan_drawdown_net signature stays additive.
    draw_rate = marginal_rate(cpp + pension + net_shortfall, year_brackets)

    # `other_taxable_income` is the taxable income the progressive draw stacks
    # on and the non-OAS part of the OAS-clawback base (#363). OAS is kept OUT
    # of it: the clawback base adds gross OAS separately (plan_drawdown_net), so
    # folding OAS in here would double-count it.
    other_taxable_income = cpp + pension

    # Issue #618: the bracket-fill drawdown ceiling. OAS IS taxable income for
    # bracket purposes — it is RECEIVED net of the 15% recovery tax, but the tax
    # brackets still see the full taxable OAS — so the RRSP-draw ceiling the
    # 'rrsp_bracket_fill' order fills against sits on top of CPP + pension + OAS.
    # Excluding OAS (the pre-#618 behaviour) let the bracket-fill order over-draw
    # by the OAS slice. This OAS-inclusive base feeds ONLY the bracket-fill
    # headroom (ws.drawdown_bracket_fill_base -> plan_drawdown_net), never the
    # progressive/clawback base above. DP#13: an explicit
    # `retirement.bracket_fill_target` wins; absent that, auto-detect from the
    # year-versioned brackets (DP#20) rather than hardcoding a dollar figure —
    # and the ceiling is the top of the bracket the OAS-INCLUSIVE income sits in.
    bracket_fill_base = other_taxable_income + oas
    bracket_fill_target = ret.get('bracket_fill_target')
    if bracket_fill_target is None:
        bracket_fill_target = bracket_ceiling(bracket_fill_base, year_brackets)

    # Issue #363 PR 4: the per-spouse pricing bases. Only meaningful when BOTH
    # spouses are retired — that is when the discretionary draw splits across
    # their two SEPARATE bracket sets (Canada has no joint filing). Each spouse's
    # own progressive base is their own CPP + pension; their bracket-fill headroom
    # sits on their own OAS-inclusive base (same #618 rule as the household one),
    # and an explicit `bracket_fill_target` override applies per spouse just as it
    # does household-wide. In a one-retiree year the split is off and these are
    # unused (the household schedule above prices the draw exactly as pre-PR-4).
    two_member_split = both_retired  # (computed above for the split elections)
    other_taxable_primary = ri_p.cpp + ri_p.pension
    other_taxable_spouse = ri_s.cpp + ri_s.pension
    bracket_fill_base_primary = other_taxable_primary + ri_p.oas
    bracket_fill_base_spouse = other_taxable_spouse + ri_s.oas
    if ret.get('bracket_fill_target') is not None:
        bracket_target_primary = bracket_target_spouse = ret['bracket_fill_target']
    else:
        bracket_target_primary = bracket_ceiling(bracket_fill_base_primary, year_brackets)
        bracket_target_spouse = bracket_ceiling(bracket_fill_base_spouse, year_brackets)
    # Deprecated per-spouse flat fallbacks (consulted only when no brackets are
    # supplied — never in the live fold, which always passes year_brackets).
    draw_rate_primary = marginal_rate(other_taxable_primary, year_brackets)
    draw_rate_spouse = marginal_rate(other_taxable_spouse, year_brackets)

    # -- RRIF minimum-withdrawal rates (mandatory decumulation) --
    # Once the RRSP is a RRIF (required by age 71), the CRA age factor forces
    # a minimum taxable withdrawal each year. We pass the age-based *rate*
    # (0 before conversion) so the pure step applies it to the Jan-1 balance.
    # DP#32/#621: an explicit `rrif_conversion_age: 0` is bad input (no RRSP
    # converts to a RRIF at age 0), but it must surface as bad data, not be
    # silently coerced to the statutory default via truthiness.
    _conv_age = ret.get('rrif_conversion_age')
    conv_age = 71 if _conv_age is None else _conv_age
    rrif_rates = _get_rrif_rates(year=sim_year)

    def _rrif_rate(m, retired):
        if not m or not retired:
            return 0.0
        by = m.get('birth_year', 0)
        if not by:
            return 0.0
        age = sim_year - by
        if age < conv_age:
            return 0.0
        return rrif_rates.get(age, 0.20)

    rrif_min_rate_primary = _rrif_rate(primary, p_retired)
    rrif_min_rate_spouse = _rrif_rate(spouse, s_retired)
    # Issue #825: the tax on the forced RRIF minimum is no longer a flat
    # placeholder rate computed here. apply_rrif_minimum now prices each spouse's
    # forced slice through the SAME progressive re-bracketing + OAS-clawback
    # machinery as the discretionary drawdown (price_forced_rrif_tax), using the
    # per-spouse bases already written below.

    ws.cpp_income = cpp
    ws.oas_income = oas
    ws.pension_income = pension
    ws.drawdown_order = drawdown_order
    ws.liquidate_to_target = liquidate_to_target
    # Issue #674: per-member flags so callers can also zero *_earned_income
    # (a retired member accrues no new RRSP room from employment they no
    # longer have) without re-deriving retirement status themselves.
    ws.primary_retired = p_retired
    ws.spouse_retired = s_retired
    ws.rrif_min_rate_primary = rrif_min_rate_primary
    ws.rrif_min_rate_spouse = rrif_min_rate_spouse
    # Issue #1001: drawdown_net_target is set AFTER the per-spouse bases below,
    # where the forced RRIF minimum's after-tax is priced and netted in.
    # Issue #758: the effective retirement spending target + the
    # retirement-phase flag, so apply_solvency can use the correct
    # spending figure in retirement instead of the working-phase
    # `living_costs` (which double-counts spending the drawdown funds).
    ws.any_retired = any_retired
    ws.retirement_spending_target = eff_retirement_spending_target
    ws.retiree_marginal_rate = draw_rate
    ws.drawdown_bracket_target = bracket_fill_target
    # Issue #1033 round-2 NEW-1: record whether the target was an EXPLICIT
    # retirement.bracket_fill_target election (DP#13) so the s.20(1)(c) routing
    # in apply_retirement_drawdown does not overwrite a declared ceiling.
    ws.drawdown_bracket_target_explicit = ret.get('bracket_fill_target') is not None
    ws.drawdown_other_taxable_income = other_taxable_income
    # Issue #618: OAS-inclusive base for the bracket-fill headroom only.
    ws.drawdown_bracket_fill_base = bracket_fill_base
    # Issue #363 PR 2: hand the gross OAS + recovery-tax threshold to the
    # drawdown rule so it folds the OAS clawback the taxable draw triggers into
    # the draw. covered_net above nets FULL OAS (the base clawback on CPP alone
    # is structurally zero — max CPP << the ~$95k threshold), so net_shortfall
    # is the full-OAS shortfall and the drawdown grosses up to REPLACE whatever
    # OAS the draw claws back. Booking the reduced OAS lives in the drawdown rule
    # (where the draw — and thus the clawback base — is known), not here (this
    # runs before the draw is sized): part (i), "feed the taxable draw into the
    # clawback base", realized at the one point the draw exists.
    ws.drawdown_oas_gross = oas
    ws.drawdown_oas_threshold = oas_threshold
    # Issue #363 PR 4: per-spouse pricing bases + the split gate.
    ws.drawdown_two_member_split = two_member_split
    ws.drawdown_other_taxable_income_primary = other_taxable_primary
    ws.drawdown_other_taxable_income_spouse = other_taxable_spouse
    ws.drawdown_oas_gross_primary = ri_p.oas
    ws.drawdown_oas_gross_spouse = ri_s.oas
    ws.drawdown_bracket_target_primary = bracket_target_primary
    ws.drawdown_bracket_target_spouse = bracket_target_spouse
    ws.drawdown_bracket_fill_base_primary = bracket_fill_base_primary
    ws.drawdown_bracket_fill_base_spouse = bracket_fill_base_spouse
    ws.retiree_marginal_rate_primary = draw_rate_primary
    ws.retiree_marginal_rate_spouse = draw_rate_spouse

    # Issue #1001: price the forced RRIF minimum's AFTER-TAX proceeds (pure —
    # no side effects on ws.oas_income / balances; apply_rrif_minimum books the
    # tax + clawback + reinvest exactly once, later) and net them into the
    # discretionary drawdown target. The mandatory RRIF minimum is cash the
    # household RECEIVES in retirement, so it covers part of the net spending
    # target before any discretionary TFSA/non-reg draw is sized — fixing the
    # defect where the household drew tax-free TFSA it did not need while the
    # already-taxed RRIF surplus was reinvested into taxable non-reg for 25
    # years. The per-spouse forced slice re-brackets on that spouse's own
    # CPP/pension base (prior_taxable_draw=0 here — the discretionary draw has
    # not run yet; it draws TFSA-first so its taxable slice is ~0, making this
    # pricing consistent with apply_rrif_minimum's post-draw pricing in the
    # common case). Both the income tax and the incremental OAS recovery tax
    # reduce the after-tax proceeds available to cover spending.
    forced_rrif_after_tax = 0.0
    if net_shortfall_pre_rrif > 0.0:
        from countries.canada.retirement_transition import price_forced_rrif_tax
        forced_primary = (ws.opening_rrsp_balance * rrif_min_rate_primary
                          if rrif_min_rate_primary > 0 else 0.0)
        forced_spouse = ((ws.opening_spouse_rrsp_balance
                          + ws.opening_spousal_rrsp_balance) * rrif_min_rate_spouse
                         if rrif_min_rate_spouse > 0 else 0.0)
        if (forced_primary > 0.0 or forced_spouse > 0.0):
            tax_p, claw_p = price_forced_rrif_tax(
                other_taxable_income=other_taxable_primary,
                oas_gross=ri_p.oas,
                prior_taxable_draw=0.0,
                forced_taxable=forced_primary,
                brackets=ctx.year_brackets,
                oas_clawback_threshold=oas_threshold,
                flat_rate=draw_rate_primary)
            tax_s, claw_s = price_forced_rrif_tax(
                other_taxable_income=other_taxable_spouse,
                oas_gross=ri_s.oas,
                prior_taxable_draw=0.0,
                forced_taxable=forced_spouse,
                brackets=ctx.year_brackets,
                oas_clawback_threshold=oas_threshold,
                flat_rate=draw_rate_spouse)
            forced_rrif_after_tax = (
                (forced_primary + forced_spouse)
                - (tax_p + tax_s))
            # (The incremental OAS recovery tax claw_p+claw_s is NOT subtracted
            # here: it is booked in apply_rrif_minimum as ws.oas_income -= claw,
            # and OAS income already flows into covered_net above and the
            # solvency `available` below, so subtracting it here would
            # double-count the clawback's hit to spending capacity.)
            if forced_rrif_after_tax > 0.0:
                net_shortfall = max(0.0, net_shortfall_pre_rrif
                                    - forced_rrif_after_tax)
    ws.drawdown_net_target = net_shortfall
    ws.forced_rrif_after_tax = forced_rrif_after_tax
    ws.drawdown_net_target_pre_rrif = net_shortfall_pre_rrif
    return True
