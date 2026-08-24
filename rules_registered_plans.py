"""Registered-plan program rules with their own statutory machinery.

``lira_lif`` (locked-in transfer + the LIF minimum/maximum schedule) and
``resp`` (contributions, CESG/QESI grants, EAP wind-down, plan collapse).
Both are government programs with their own eligibility calendar and their own
lifetime caps -- DP#10 -- rather than plain compounding, which is why they sit
here and not in ``rules_growth``.

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule
from rules_growth import _blended_pot_rate


@rule('lira_lif')
def apply_lira_lif(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """CRI/LIRA growth + conversion to LIF at the statutory age, and LIF
    mandatory min/max withdrawals once converted (issue #230/#343).
    Independent of every other rule this year (locked-in accounts are not
    touched by contributions/deductions); must run before ``resp`` only
    because that is this file's declared narrative order, not a real
    dependency -- kept immediately after growth per the original engine's
    section order (DP#26: preserving relative order keeps the refactor
    byte-identical to what it replaces).

    Issue #912: the LIRA and LIF grow at the same per-pot rate machinery the
    rrsp/tfsa pots use (``_blended_pot_rate``) so a declared foreign-equity
    composition drags their return via #641's ``registered_wht_drag`` -- LIRA/
    LIF are locked-in RETIREMENT accounts, so their foreign holdings carry the
    RRSP US-treaty exemption (leak like an RRSP's). Absent a declared lira/lif
    composition (and override/fee) the blended rate IS the flat
    ``ctx.investment_return`` (golden no-op, DP#32). The conversion year itself
    does not grow the balance (``convert_to_lif`` is a transfer, not a return),
    so no drag applies there.
    """
    from simulation_state import _get_lif_conversion_provider

    lif_withdrawal = 0.0
    new_lira_balance = ws.opening_lira_balance
    new_lif_balance = ws.opening_lif_balance
    new_lif_birth_year = ws.opening_lif_birth_year
    new_lif_jurisdiction = ws.opening_lif_jurisdiction
    new_lif_reference_rate = ws.opening_lif_reference_rate

    fired = False
    lif_provider = None
    if ((ws.opening_lira_balance > 0 and ws.opening_lira_birth_year > 0)
            or (ws.opening_lif_balance > 0 and ws.opening_lif_birth_year > 0)):
        lif_provider = _get_lif_conversion_provider()

    # Issue #1002: the LIF statutory MAXIMUM withdrawal for the year, computed
    # on the same fund the forced-minimum path builds (opening balance for an
    # existing LIF, the just-converted balance in a LIRA->LIF conversion year).
    # Stays 0.0 when there is no LIF activity this year. Stored on ws so the
    # discretionary drawdown (apply_retirement_drawdown, later in the fold) can
    # cap the discretionary LIF draw at ``max - lif_withdrawal`` -- preventing
    # the total (forced min + discretionary) from exceeding the annual ceiling
    # the forced path already enforces on its own slice.
    lif_maximum_withdrawal = 0.0

    if ws.opening_lira_balance > 0 and ws.opening_lira_birth_year > 0:
        # Issue #343: compare against the absolute calendar year, not the
        # 0-based projection index.
        # Issue #708: the conversion year is event-driven — the EARLIER of an
        # elected conversion date (lira.conversion_date) and the mandatory
        # age-71 backstop. An absent election (opening_lira_conversion_year
        # == 0) yields the backstop, byte-identical to the pre-#708 path. The
        # jurisdiction's earliest-permitted conversion age is enforced inside
        # the provider (Quebec: no minimum, sourced; federal/Ontario: rejected
        # rather than guessed).
        election_year = ws.opening_lira_conversion_year
        convert_year = lif_provider.lif_conversion_year(
            ws.opening_lira_birth_year,
            ws.opening_lira_jurisdiction,
            election_year if election_year > 0 else None,
        )
        if ctx.calendar_year >= convert_year:
            account = lif_provider.make_locked_in_account(
                balance=ws.opening_lira_balance,
                birth_year=ws.opening_lira_birth_year,
                jurisdiction=ws.opening_lira_jurisdiction,
            )
            lif_fund, depleted_account = account.convert_to_lif(
                ctx.calendar_year, reference_rate=ws.opening_lira_reference_rate)
            new_lira_balance = 0.0
            new_lif_balance = lif_fund.balance
            new_lif_birth_year = lif_fund.owner_birth_year
            new_lif_jurisdiction = lif_fund.jurisdiction
            new_lif_reference_rate = lif_fund.reference_rate
            # Issue #1002: the freshly-converted LIF is a live account this
            # year -- record its statutory maximum so a discretionary draw
            # ordered against it (drawdown_order front-loads 'lif') is capped
            # rather than over-drawing. No forced minimum is taken in the
            # conversion year (the block below is gated on opening_lif_balance
            # > 0, which is 0 here), so the whole ceiling is discretionary room.
            lif_maximum_withdrawal = lif_fund.maximum_withdrawal(ctx.calendar_year)
            fired = True
        else:
            lira_rate = _blended_pot_rate(ctx, 'lira', ws.opening_lira_balance)
            new_lira_balance = ws.opening_lira_balance * (1 + lira_rate)
            fired = True

    if ws.opening_lif_balance > 0 and ws.opening_lif_birth_year > 0 and new_lira_balance == 0:
        fund = lif_provider.make_lif_fund(
            balance=new_lif_balance if new_lif_balance > 0 else ws.opening_lif_balance,
            owner_birth_year=ws.opening_lif_birth_year,
            reference_rate=ws.opening_lif_reference_rate,
            jurisdiction=ws.opening_lif_jurisdiction,
        )
        lif_withdrawal = fund.minimum_withdrawal(ctx.calendar_year)
        max_withdrawal = fund.maximum_withdrawal(ctx.calendar_year)
        if lif_withdrawal > max_withdrawal and max_withdrawal > 0:
            lif_withdrawal = max_withdrawal
        # Issue #1002: record the statutory maximum (on the opening fund) so the
        # discretionary drawdown caps the residual at ``max - lif_withdrawal``.
        lif_maximum_withdrawal = max_withdrawal
        actual_withdrawal, updated_fund = fund.withdraw(lif_withdrawal, ctx.calendar_year)
        lif_rate = _blended_pot_rate(ctx, 'lif', updated_fund.balance)
        _, grown_fund = updated_fund.grow(lif_rate)
        new_lif_balance = grown_fund.balance
        new_lif_birth_year = grown_fund.owner_birth_year
        new_lif_jurisdiction = grown_fund.jurisdiction
        new_lif_reference_rate = grown_fund.reference_rate
        fired = True
    elif ws.opening_lif_balance > 0 and ws.opening_lira_balance > 0:
        # Defensive: LIF exists but LIRA hasn't converted yet.
        lif_rate = _blended_pot_rate(ctx, 'lif', ws.opening_lif_balance)
        new_lif_balance = ws.opening_lif_balance * (1 + lif_rate)
        fired = True

    ws.lif_withdrawal = lif_withdrawal
    ws.lif_maximum_withdrawal = lif_maximum_withdrawal
    ws.new_lira_balance = new_lira_balance
    ws.new_lif_balance = new_lif_balance
    ws.new_lif_birth_year = new_lif_birth_year
    ws.new_lif_jurisdiction = new_lif_jurisdiction
    ws.new_lif_reference_rate = new_lif_reference_rate
    return fired

@rule('resp')
def apply_resp(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """RESP: contribute, grow, then wind down as EAP/PSE across the study
    window, or collapse via AIP once the window closes with money left
    over (issue #578, DP#1/DP#28: eligibility is date-computed from
    birth_year). Depends on ``contributions`` only via ``ctx.resp_data``
    (pre-computed CESG/QESI grants), not on any other rule's ``ws`` output.
    """
    from countries.canada.resp_rules import (
        resp_study_window_for_child,
        resp_annual_withdrawal, resp_collapse_aip,
    )

    resp_balances = ws.opening_resp_balances
    resp_contributions = ws.opening_resp_contributions
    resp_cesg = ws.opening_resp_cesg
    resp_qesi = ws.opening_resp_qesi
    n_resp_children = len(resp_balances)
    new_resp_balances = []
    new_resp_contributions = []
    new_resp_cesg = []
    new_resp_qesi = []
    resp_eap_paid = 0.0
    resp_pse_paid = 0.0
    resp_aip_tax = 0.0
    fired = False

    for i in range(n_resp_children):
        bal = resp_balances[i]
        contrib_i = resp_contributions[i] if i < len(resp_contributions) else 0.0
        cesg_i = resp_cesg[i] if i < len(resp_cesg) else 0.0
        qesi_i = resp_qesi[i] if i < len(resp_qesi) else 0.0

        if ctx.resp_data and i < len(ctx.resp_data):
            ch_contrib = ctx.resp_data[i].get('contribution', 0)
            ch_cesg = ctx.resp_data[i].get('cesg', 0)
            ch_qesi = ctx.resp_data[i].get('qesi', 0)
        elif not ctx.resp_data:
            ch_contrib = ws.resp_alloc / n_resp_children
            ch_cesg = 0.0
            ch_qesi = 0.0
        else:
            ch_contrib = ch_cesg = ch_qesi = 0.0

        if ch_contrib or ch_cesg or ch_qesi:
            fired = True

        bal = (bal + ch_contrib + ch_cesg + ch_qesi) * (1 + ctx.investment_return)
        contrib_i += ch_contrib
        cesg_i += ch_cesg
        qesi_i += ch_qesi
        earnings_i = max(0.0, bal - contrib_i - cesg_i - qesi_i)

        birth_year = ctx.config.children[i].get('birth_year', 0) if i < len(ctx.config.children) else 0
        if not birth_year and i < len(ctx.config.children):
            age = ctx.config.children[i].get('age', 0)
            birth_year = (ctx.config.start_year - age) if age else 0

        if birth_year > 0:
            # Issue #714: THIS child's declared study window (people[].
            # study_periods[] -> child['study_periods']) when they gave one,
            # and only otherwise the household-wide age assumption. Before
            # this, study_periods was mapped and read by nobody, so every
            # beneficiary wound down on the GLOBAL assumptions.resp.
            # study_start_age regardless of when they actually study.
            #
            # Computed ONCE and used by all three predicates below. It used to
            # be derived here and then independently re-derived inside
            # is_resp_study_year()/has_aged_out_of_resp_study() from the same
            # inputs -- three spellings of one window, which is how a per-child
            # window could be added to the config and silently disagree with
            # the one the withdrawal maths actually used (DP#9).
            child_cfg = ctx.config.children[i] if i < len(ctx.config.children) else {}
            first_year, last_year = resp_study_window_for_child(
                child_cfg, birth_year,
                ctx.config.resp_study_start_age, ctx.config.resp_study_duration_years)
            in_study_year = first_year <= ctx.calendar_year <= last_year
            has_aged_out = ctx.calendar_year > last_year

            if ctx.config.resp_used_for_education and in_study_year:
                years_left = last_year - ctx.calendar_year + 1
                draw = resp_annual_withdrawal(contrib_i, cesg_i, qesi_i, earnings_i, years_left)
                contrib_i -= draw['contributions_withdrawn']
                cesg_i -= draw['cesg_withdrawn']
                qesi_i -= draw['qesi_withdrawn']
                earnings_i -= draw['earnings_withdrawn']
                bal -= (draw['pse'] + draw['eap'])
                resp_pse_paid += draw['pse']
                resp_eap_paid += draw['eap']
                fired = True
            elif bal > 1e-6 and (
                    has_aged_out
                    or (not ctx.config.resp_used_for_education and ctx.calendar_year >= first_year)):
                collapse = resp_collapse_aip(cesg_i, qesi_i, earnings_i, ctx.primary_marginal_rate)
                resp_pse_paid += contrib_i
                resp_aip_tax += collapse['aip_tax']
                bal = 0.0
                contrib_i = 0.0
                cesg_i = 0.0
                qesi_i = 0.0
                earnings_i = 0.0
                fired = True

        new_resp_balances.append(max(0.0, bal))
        new_resp_contributions.append(max(0.0, contrib_i))
        new_resp_cesg.append(max(0.0, cesg_i))
        new_resp_qesi.append(max(0.0, qesi_i))

    ws.new_resp_balances = new_resp_balances
    ws.new_resp_contributions = new_resp_contributions
    ws.new_resp_cesg = new_resp_cesg
    ws.new_resp_qesi = new_resp_qesi
    ws.resp_eap_paid = resp_eap_paid
    ws.resp_pse_paid = resp_pse_paid
    ws.resp_aip_tax = resp_aip_tax
    return fired
