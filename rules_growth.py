"""Growth rules: every pot compounds, each at the rate that pot actually earns.

``registered_growth`` compounds at the portfolio's blended per-pot rate
(``_blended_pot_rate``, which also prices MER and per-account composition);
``non_reg_growth`` compounds at the DP#27 after-tax rate shifted by the non_reg
account's declared ``expected_return`` blend and ``mer`` (#291, see
``apply_non_reg_growth``); ``emergency_reserve_growth`` and ``deposit_product_growth``
deliberately do NOT -- a reserve modelled as compounding at the equity return
is not a reserve (#688), and a taken deposit product compounds at its own
declared ``rate_schedule`` (#936).

``_blended_pot_rate`` is the ONE spelling of a registered pot's rate (DP#9);
the LIRA/LIF rule (``rules_registered_plans``) and the FHSA rule
(``rules_contributions``) import it from here rather than re-spelling it. Its
override blend and MER read live in ``_pot_gross_rate`` / ``_pot_mer_rate``,
which the non_reg rule shares (#291).

Split out of ``simulation_rules.py``; the rule bodies are unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

from rule_registry import RuleContext, YearWorkingState, rule

logger = logging.getLogger(__name__)


def _pot_gross_rate(ctx: 'RuleContext', kind: str, pot_total: float) -> float:
    """Issue #823: the GROSS rate for one aggregate pot -- the balance-weighted
    blend of the per-account ``expected_return`` overrides declared on accounts
    of ``kind`` and the global ``ctx.investment_return`` for the rest of the pot.

    No override declared for ``kind`` (or an empty pot) -> exactly
    ``ctx.investment_return`` (identity-preserving for the golden run). Split
    out of ``_blended_pot_rate`` by #291 so the non_reg rule can apply the same
    blend as a shift of its after-tax rate; the arithmetic is unchanged.
    """
    if pot_total <= 0:
        return ctx.investment_return
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
    return gross


def _pot_mer_rate(ctx: 'RuleContext', kind: str) -> Optional[float]:
    """Issue #691/#136/#291: the declared MER rate of pot ``kind``, or ``None``
    when no account of that kind declared one.

    ``None`` (absent) and ``0.0`` (a declared fee-free fact) are distinct
    (DP#32). An entry that exists but carries no ``mer_rate`` is malformed
    input: ``mer_rate`` is read by subscript so it raises ``KeyError`` rather
    than reading as a 0% fee.
    """
    mer_drag = ctx.config.account_mer_drag
    fee = mer_drag.get(kind) if mer_drag else None
    if fee is None:
        return None
    return fee['mer_rate']


def _blended_pot_rate(ctx: 'RuleContext', kind: str, pot_total: float) -> float:
    """Issue #823/#691: the growth rate for one aggregate REGISTERED pot
    (rrsp/tfsa/fhsa/lira/lif).

    The non_reg pot does not use it (#291): ``apply_non_reg_growth`` composes
    the same two helpers (``_pot_gross_rate``, ``_pot_mer_rate``) onto its
    DP#27 after-tax rate, without the registered-only #641 WHT drag.

    Two per-account overrides, composed, both balance-weighted into the pot:

    - #823 ``expected_return``: if the household declared one on any account of
      ``kind``, the pot's GROSS rate is a balance-weighted blend of the override
      rate and the global ``ctx.investment_return``; otherwise the gross rate is
      the global rate (today's behaviour).
    - #691 ``mer``: a declared per-account fee is subtracted from that gross
      rate -- ``net = gross - mer_rate`` -- so a declared fee reduces the
      compounded balance. An account carrying BOTH grows at
      (expected_return - mer): the two terms simply add here.

    Issue #136: the MER is a RATE (``mer_rate``), not a frozen weighted sum.
      The fee is ``mer_rate * pot_total`` each year — dynamic. Before #136 the
      fee numerator (``weighted_mer_sum``) was frozen at load time, so (a) a $0
      opening-balance account paid zero fee forever even after contributions
      funded it, and (b) a funded account's effective fee rate decayed toward
      zero as the pot grew. Now ``mer_rate`` is a constant rate applied to the
      current pot total, so the fee grows proportionally — no decay, and a $0
      account's fee is not silently zero.

    Absence is a strict no-op: no override and no fee -> the global rate is
    returned unchanged (the golden household declares neither). See
    ``apply_registered_growth`` for the approximation caveat (the blend uses the
    DECLARED opening balance of the flagged accounts, not a per-account
    sub-balance tracked through the run -- a first-order approximation the issue
    itself flags as numerically small).
    """
    if pot_total <= 0:
        return ctx.investment_return
    # Gross rate: the #823 expected_return blend, or the global rate when no
    # account of this kind declared one (identity-preserving for the golden run).
    gross = _pot_gross_rate(ctx, kind, pot_total)
    # Issue #691/#136: subtract the MER rate of fee-flagged accounts in this
    # pot from the gross rate. The MER is a constant rate (not a frozen
    # weighted sum divided by the pot total), so the fee is mer_rate *
    # pot_total each year — dynamic, not decaying. Absent (no mer_drag entry)
    # or fee-free (mer_rate == 0) -> no change, so the gross rate is returned
    # untouched (golden no-op, DP#32).
    mer_rate = _pot_mer_rate(ctx, kind)
    if mer_rate:
        gross -= mer_rate
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
    gross rate registered accounts use -- shifted by the non_reg account's
    own declared ``expected_return`` and ``mer`` (#291). Depends on
    ``contributions``.

    Issue #291 -- the convention. A fund's MER is paid inside the fund, out of
    its TOTAL return, before anything is distributed: the declared
    distribution yield is the net distribution the investor actually receives,
    so the fee comes out of the deferred capital-appreciation term at its full
    rate. A per-account ``expected_return`` replaces the gross total return for
    its balance-weighted share of the pot (the #823 blend), and that
    difference is likewise capital appreciation. ACB is unaffected (DP#19).

    The derivation. ``_non_reg_after_tax_return_for`` returns
    ``atr(g) = after_tax_yield + (g - declared_yield)`` and ``after_tax_yield``
    does not depend on ``g``. Evaluating it at the fee-net, override-blended
    gross ``g' = blend - mer`` is therefore exactly::

        atr(g') = atr(g) + (blend - g) - mer

    i.e. ``atr + (blended_gross - gross) - mer``, which is what this rule
    computes -- an exact linear shift, not an approximation. It is applied
    here rather than inside ``_non_reg_after_tax_return_for`` because the
    blend is balance-weighted on the LIVE pot, and that function must never
    read a balance (#575/#583).

    The shift is applied unconditionally: no float equality between the
    after-tax rate and ``ctx.investment_return`` decides whether a declared
    input is read (the pre-#291 gate did, and the fold's after-tax rate is
    below gross for any taxed yield, so both inputs were silently dropped).
    With neither input declared the shift is exactly ``+ 0.0`` (golden no-op).

    The Smith-Manoeuvre sleeve grows at ``ws.taxable_after_tax_rate``, the
    UNSHIFTED shared taxable rate: it is not a declared account, so it never
    inherits the non_reg account's fee or return override.
    """
    if ctx.non_reg_after_tax_return is not None:
        taxable_rate = ctx.non_reg_after_tax_return
    else:
        taxable_rate = ctx.investment_return
        logger.warning(
            "non_reg_after_tax_return not provided; falling back to flat investment_return=%.4f. "
            "For accurate non-reg projections, provide non_reg_after_tax_return "
            "from portfolio composition data (DP#27).",
            ctx.investment_return
        )
    # The shared DP#27 taxable rate the SM sleeve reads -- deliberately WITHOUT
    # the non_reg account's own declarations (#291).
    ws.taxable_after_tax_rate = taxable_rate
    # #291/#823: the declared expected_return blend shifts the rate by exactly
    # (blended gross - global gross); absent -> (ir - ir) == 0.0.
    pot_rate = taxable_rate + (_pot_gross_rate(ctx, 'non_reg', ws.new_nonreg_bal)
                               - ctx.investment_return)
    # #291/#691: the declared MER comes out of total return (see docstring).
    # Absent -> None -> skipped; a declared 0.0 subtracts nothing.
    mer_rate = _pot_mer_rate(ctx, 'non_reg')
    if mer_rate is not None:
        pot_rate -= mer_rate
    pre = ws.new_nonreg_bal
    ws.new_nonreg_bal *= (1 + pot_rate)
    # ACB does NOT grow with returns (it's cost basis)
    return pre > 0 and pot_rate != 0

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
