"""Growth rules: every pot compounds, each at the rate that pot actually earns.

``registered_growth`` / ``non_reg_growth`` compound at the portfolio's blended
per-pot rate (``_blended_pot_rate``, which also prices MER and per-account
composition); ``emergency_reserve_growth`` and ``deposit_product_growth``
deliberately do NOT -- a reserve modelled as compounding at the equity return
is not a reserve (#688), and a taken deposit product compounds at its own
declared ``rate_schedule`` (#936).

``_blended_pot_rate`` is the ONE spelling of a pot's rate (DP#9); the LIRA/LIF
rule (``rules_registered_plans``) and the FHSA rule (``rules_contributions``)
import it from here rather than re-spelling it.

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

from rule_registry import RuleContext, YearWorkingState, rule

logger = logging.getLogger(__name__)


def _blended_pot_rate(ctx: 'RuleContext', kind: str, pot_total: float) -> float:
    """Issue #823/#691: the growth rate for one aggregate pot
    (rrsp/tfsa/non_reg/...).

    Two per-account overrides, composed, both balance-weighted into the pot:

    - #823 ``expected_return``: if the household declared one on any account of
      ``kind``, the pot's GROSS rate is a balance-weighted blend of the override
      rate and the global ``ctx.investment_return``; otherwise the gross rate is
      the global rate (today's behaviour).
    - #691/#136 ``mer``: a declared per-account fee is subtracted from that
      gross rate as a FIXED DRAG RATE -- ``net = gross - fee_share * fee_rate``,
      where ``fee_share`` is the fee-accounts' share of the kind's DECLARED pot
      and ``fee_rate`` their balance-weighted average fee. The fee price stays on
      the pot's value regardless: it does NOT dilute as the pot grows, and it
      does NOT freeze to a $0 contribution when a fee account opens zero and is
      funded later (issue #136). An account carrying BOTH grows at
      (expected_return - mer): the two terms simply add here.

    Absence is a strict no-op: no override and no fee -> the global rate is
    returned unchanged (the golden household declares neither).
    """
    if pot_total <= 0:
        return ctx.investment_return
    # Gross rate: the #823 expected_return blend, or the global rate when no
    # account of this kind declared one (identity-preserving for the golden run).
    gross = ctx.investment_return
    overrides = ctx.config.account_return_overrides
    entry = overrides.get(kind) if overrides else None
    if entry:
        override_balance = entry.get('override_balance', 0.0)
        weighted_rate_sum = entry.get('weighted_rate_sum', 0.0)
        if override_balance > 0:
            gross = (weighted_rate_sum
                     + max(0.0, pot_total - override_balance) * ctx.investment_return
                     ) / pot_total
    # Issue #691/#136: the MER drag is a FIXED RATE (fee_share x fee_rate), not
    # a frozen dollar snapshot divided by the current pot total. Under the old
    # spelling (weighted_mer_sum / pot_total) the numerator stayed at the
    # DECLARED opening balance: a zero-balance fee account contributed 0 to it
    # forever (its declared fee never applied once funded), and a funded
    # segment diluted as the pot grew. Storing the DRAG AS A RATE prices the
    # fee on the pot's whole value every year: no freezing at opening, no
    # dilution, and the golden no-op (no fee declared -> fee is None -> gross
    # unchanged) holds (DP#32). Fee-free (fee_rate == 0 or fee_share == 0) is a
    # strict no-op (DP#32: an explicit 0.0 fee is a declared fact that moves no
    # rate).
    mer_drag = ctx.config.account_mer_drag
    fee = mer_drag.get(kind) if mer_drag else None
    if fee:
        share = fee.get('fee_share', 0.0)
        rate = fee.get('fee_rate', 0.0)
        if share > 0 and rate != 0:
            gross -= share * rate
    # Issue #641: subtract the foreign-withholding-tax drag of this REGISTERED
    # pot's declared holdings (rrsp/tfsa) -- the one tax that leaks from an
    # otherwise tax-sheltered account. Absent (no registered composition, or a
    # domestic/fixed-income-only pot) -> no entry -> gross unchanged (golden
    # no-op, DP#32). non_reg never appears here (its WHT is recoverable and its
    # composition reaches growth via non_reg_after_tax_return -- no double count).
    wht_drag = ctx.registered_wht_drag
    if wht_drag:
        gross -= wht_drag.get(kind, 0.0)
    return gross

@rule('registered_growth')
def apply_registered_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow RRSP/TFSA balances at the gross (tax-sheltered) rate.
    Depends on ``contributions`` for the post-contribution balances.

    Issue #823: if the household declared a per-account ``expected_return``
    override on any rrsp/tfsa account, that pot grows at a BALANCE-WEIGHTED
    blend of the override rate and the global ``investment_return`` -- so a
    flagged account (e.g. Fonds FTQ at 7.3%) grows at its own rate while the
    rest of the pot uses the global rate. No override declared -> the global
    rate (today's behaviour, golden). The blend uses the DECLARED opening
    balance of the flagged accounts (from the contract) against the pot's
    current post-contribution total -- a first-order approximation the issue
    itself flags as numerically small (7.3% vs 7% on ~$34k ~$100/yr); it is
    not a second growth model, just a per-pot rate (DP#21: the return model
    stays the single source of the global rate; this is an override ON it).
    """
    rrsp_rate = _blended_pot_rate(ctx, 'rrsp',
                                  ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal)
    tfsa_rate = _blended_pot_rate(ctx, 'tfsa',
                                  ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal)
    pre = ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal + ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
    ws.new_rrsp_bal *= (1 + rrsp_rate)
    ws.new_spousal_rrsp_bal *= (1 + rrsp_rate)
    ws.new_spouse_rrsp_bal *= (1 + rrsp_rate)
    ws.new_tfsa_p_bal *= (1 + tfsa_rate)
    ws.new_tfsa_sp_bal *= (1 + tfsa_rate)
    return pre > 0 and (ctx.investment_return != 0 or rrsp_rate != 0 or tfsa_rate != 0)

@rule('non_reg_growth')
def apply_non_reg_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """DP#27: non-reg investments grow at the income-type-specific
    after-tax rate (portfolio composition + marginal rate), not the flat
    gross rate registered accounts use. Depends on ``contributions``.
    """
    if ctx.non_reg_after_tax_return is not None:
        non_reg_growth_rate = ctx.non_reg_after_tax_return
    else:
        non_reg_growth_rate = ctx.investment_return
        logger.warning(
            "non_reg_after_tax_return not provided; falling back to flat investment_return=%.4f. "
            "For accurate non-reg projections, provide non_reg_after_tax_return "
            "from portfolio composition data (DP#27).",
            ctx.investment_return
        )
    # Issue #823: blend a per-account expected_return override on non_reg
    # accounts into the pot rate (see _blended_pot_rate). Applied on the
    # fallback (flat investment_return) path; when portfolio composition
    # supplies an after-tax rate (the DP#27 normal path), the override is not
    # blended in -- threading a per-account pre-tax override through the
    # portfolio after-tax adjustment is a future refinement. FTQ (the issue's
    # subject) lives in RRSP, not non_reg, so this is not the load-bearing
    # path for #823.
    if non_reg_growth_rate == ctx.investment_return:
        non_reg_growth_rate = _blended_pot_rate(ctx, 'non_reg', ws.new_nonreg_bal)
    ws.non_reg_growth_rate = non_reg_growth_rate
    pre = ws.new_nonreg_bal
    ws.new_nonreg_bal *= (1 + non_reg_growth_rate)
    # ACB does NOT grow with returns (it's cost basis)
    return pre > 0 and non_reg_growth_rate != 0

@rule('emergency_reserve_growth')
def apply_emergency_reserve_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Grow the emergency reserve at its OWN declared instrument rate --
    never the portfolio's (issue #688).

    **A reserve modelled as compounding at the equity return is not a
    reserve.** That is the whole point of the field: the household is
    choosing to hold money OUT of the market, and the cost of that choice
    (forgone return) is exactly what the ``emergency_reserve_months`` sweep
    exists to price against the benefit (not being forced to sell at the
    bottom). Growing the sleeve at ``ctx.investment_return`` would make the
    reserve free, and the sweep would report that holding 24 months of cash
    costs nothing -- a confident, wrong, and dangerous number.

    DP#32: ``emergency_reserve_rate`` is a REQUIRED field of the schema
    block, so a declared reserve always has a rate. A rate of exactly 0 (a
    chequing account paying nothing) is a legitimate, representable value and
    is honoured as such -- it is not treated as "unset" and quietly upgraded
    to some assumed cash yield.

    Runs with the growth rules (it is a balance that compounds) but is
    deliberately not one of them in substance. Depends on nothing any other
    rule writes; the reserve's opening balance is the Jan-1 sleeve carved out
    by ``SimState.initial`` (#688) or left by last year's ``solvency`` rule.
    """
    opening = ws.opening_emergency_reserve
    if opening <= 0:
        ws.new_emergency_reserve = opening
        return False

    rate = ctx.config.emergency_reserve_rate
    if rate is None:
        # Only reachable for a hand-built SimulationConfig that seeded a
        # reserve balance directly without declaring the policy that governs
        # it. Refuse rather than pick a rate: guessing here is precisely the
        # "plausible number from absent data" this codebase exists to reject
        # (DP#32). Every contract-sourced config has the rate, because the
        # schema requires it whenever the block is present.
        raise ValueError(
            f"SimState carries a ${opening:,.2f} emergency reserve but "
            f"SimulationConfig.emergency_reserve_rate is None -- the rate its "
            f"declared instrument earns was never supplied, so there is no "
            f"honest rate to compound it at (#688, DP#32). Declare "
            f"assumptions.emergency_reserve.rate (0 is a valid answer: a "
            f"chequing account that pays nothing)."
        )

    ws.new_emergency_reserve = opening * (1 + rate)
    return rate != 0

def _deposit_step_duration_years(step: dict) -> Optional[float]:
    """Issue #936: how many years a rate step lasts, or None if it is
    OPEN-ENDED (the final, ongoing step). A step may declare its duration in
    ``duration_years`` or ``duration_days`` (730 days = 2.0 years); a step with
    neither runs to the horizon."""
    if 'duration_years' in step:
        return step['duration_years']
    if 'duration_days' in step:
        return step['duration_days'] / 365.0
    return None


def _deposit_rate_at(schedule: list, elapsed_years: float) -> float:
    """Issue #936: the gross interest rate a deposit product pays at
    ``elapsed_years`` since funding, by walking its ordered ``rate_schedule``
    steps. A step with no duration is open-ended (the ongoing rate); once every
    termed step has elapsed, the final step's rate holds to the horizon. This
    is the generic ``rate_path``/variable shape (#936 capability #2), bound to
    one account -- one mechanism expresses a flat HISA (one open step), a
    promo teaser ([{teaser, 730d}, {base}]) and a term/GIC (a single termed
    step), which are different field values, not different concepts."""
    cumulative = 0.0
    for step in schedule:
        dur = _deposit_step_duration_years(step)
        if dur is None:
            return step['rate']
        cumulative += dur
        if elapsed_years < cumulative:
            return step['rate']
    return schedule[-1]['rate']


def _deposit_product_after_tax_rate(product: dict, elapsed_years: float,
                                    balance: float, marginal_rate: float) -> float:
    """Issue #936: the AFTER-TAX rate a taken deposit product earns THIS year
    on ``balance``, given the 0-indexed ``elapsed_years`` since funding.

    Three of the product's capabilities live here:

    * **#2 rate-step schedule.** The gross rate is whichever step of the
      product's ``rate_schedule`` contains ``elapsed_years`` (``_deposit_rate_at``
      walks the ordered steps by elapsed time). This one mechanism expresses a
      flat rate, a dated teaser->base step-down, and a fixed term alike.
    * **#3 rate_eligible_cap (OPTIONAL).** A capped-rate product pays the
      current step's rate only on the portion of the balance up to
      ``rate_eligible_cap``; any excess earns the product's ONGOING (final-step)
      rate. So the effective gross rate above the cap is the balance-weighted
      blend -- exactly how a real capped HISA ("3.00% on the first $500k, 1.50%
      above") pays. Absent cap = the whole balance earns the current step's
      rate (the trivial case for a plain HISA/GIC).
    * **#1 interest tax character.** A HISA/GIC yield is ordinary interest --
      100% taxable at the marginal rate each year as it accrues, NOT a
      deferred/50%-inclusion capital return. So the after-tax rate is the gross
      rate times ``(1 - marginal_rate)``: every dollar of yield is taxed this
      year, none deferred. (This mirrors the non-reg after-tax-return path,
      DP#27 -- interest is fully included, unlike a capital gain.)

    A non-positive balance earns nothing (returns 0.0) -- there is no cap blend
    to compute and no interest to tax.
    """
    if balance <= 0:
        return 0.0
    schedule = product['rate_schedule']
    current = _deposit_rate_at(schedule, elapsed_years)
    cap = product.get('rate_eligible_cap')
    if cap is None or balance <= cap:
        gross = current
    else:
        # The excess above the cap earns the product's ongoing (final-step)
        # rate -- the rate the schedule holds after every termed step elapses.
        ongoing = _deposit_rate_at(schedule, float('inf'))
        gross = (cap * current + (balance - cap) * ongoing) / balance

    # #936 capability #1: ordinary interest -- 100% taxable at the marginal rate.
    return gross * (1 - marginal_rate)

@rule('deposit_product_growth')
def apply_deposit_product_growth(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #936: grow a TAKEN deposit-product balance (a HISA, a term/GIC, a
    promotional teaser -- one generic mechanism) at the interest rate its
    ``rate_schedule`` prescribes for the elapsed time since funding, on the
    portion up to any ``rate_eligible_cap``, taxing the yield as ordinary
    interest.

    Sits with the other growth rules (issue #688's emergency_reserve_growth is
    the closest analog: a carved-out balance compounding at its OWN declared
    cash rate, NOT the portfolio's) but is deliberately not one of them in
    substance -- a deposit is money the household is choosing to hold at a fixed
    interest rate instead of the market, and pricing that choice is the whole
    take-vs-leave trade the optimizer ranks (#936 capability #4).

    Reads ``ctx.config.deposit_product`` -- the SINGLE product this scenario
    took (apply_overlay wrote it; None for the "leave it" baseline and every
    no-product household). None, or a 0.0 opening balance, is a strict no-op:
    the deposit balance stays 0.0 and the golden trajectory is byte-identical
    (DP#32). The opening balance was carved out of the product's funding_source
    by SimState.initial (money-conserving, capability #5).

    The interest is taxed at the funding member's marginal rate
    (``ctx.primary_marginal_rate`` -- the non-registered deposit is the
    primary's; #936 does not split a deposit across two owners) by growing at
    the after-tax rate ``_deposit_product_after_tax_rate`` returns. The fold's
    0-indexed ``ctx.year`` is the elapsed years since funding (the product is
    funded at year 0).
    """
    opening = ws.opening_deposit_product_balance
    product = ctx.config.deposit_product
    if product is None or opening <= 0:
        ws.new_deposit_product_balance = opening
        return False

    after_tax_rate = _deposit_product_after_tax_rate(
        product, ctx.year, opening, ctx.primary_marginal_rate)
    ws.deposit_product_rate = after_tax_rate
    ws.new_deposit_product_balance = opening * (1 + after_tax_rate)
    return after_tax_rate != 0
