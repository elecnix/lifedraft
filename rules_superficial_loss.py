"""The ``superficial_loss`` rule: ITA s.53(1)(c)/(f) anti-avoidance (#141).

A loss realized on a disposition of securities whose property is
re-acquired (by the taxpayer or an AFFILIATED PERSON -- s.251.1, here
subsumed by the household-level non-reg pot) within the 30-day window is
DENIED and added to the repurchased property's ACB under s.53(1)(f) -- not
destroyed. See ``superficial_loss.py`` for the arithmetic and the
annualized-window abstraction (disclosed via model_fidelity).

Position relative to the fold: runs AFTER ``solvency`` (whose forced-
liquidation waterfall is one of the two security-disposition paths whose
signed realized figure can carry a loss) and BEFORE ``capital_loss`` (whose
settlement must see the position NET of what this rule intercepted: denied
and pended losses are removed from it, released pended losses are added
back into it).

One module per government program (DP#10). The registered-sweep contract
tests ``tests/test_issue_584_rules_registry.py`` pin this rule in
``RULE_ORDER`` and require a scenario where it fires.
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule


@rule('superficial_loss')
def apply_superficial_loss(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #141: deny a superficial loss; defer it into repurchase ACB.

    The year's realized LOSS on security dispositions is the signed net of
    the two paths that can net below cost:

    - ``ws.solvency_realized_gain`` -- the forced-liquidation waterfall
      (unfloored for #140: a below-ACB forced sale realizes a genuine loss);
    - ``ws.sm_unwind_realized_gain`` -- the liquidate-to-target SM unwind.

    A negative signed net is a loss subject to the window. The REPURCHASE
    side of the window is the household's re-acquisition of securities in
    this step:

    - ``ws.non_reg_alloc`` -- this step's contribution to the household's
      non-registered pot (which includes reinvested declared-sale proceeds
      the prologue folded into the allocation, #956 bite B);
    - ``ws.principal_sale_proceeds_invested`` -- principal-residence net
      proceeds the ``principal_disposition`` rule injected into non-reg
      post-growth (#956 bite E) -- a same-step re-acquisition, counted
      conservatively;
    - ``ws.sm_readvanced`` -- the SM sleeve's readvance into its own
      investment pot (a real re-acquisition of securities on the leveraged
      side).

    Identity of property is evaluated by DECLARATION, not by ticker:
    ``ctx.config.superficial_loss_substitute_pairs`` non-empty asserts the
    household's window repurchases are of a declared non-identical
    substitute (e.g. XEQT vs VEQT), so the s.53(1)(c) limb is not met and
    the loss is allowed in its own step. ABSENT a declaration, any
    repurchase in the window is treated as identical -- the conservative
    default (DP#32: absence must not silently grant the favourable
    reading).

    Denied dollars are added to ``ws.new_nonreg_acb`` (s.53(1)(f): the
    denied loss becomes the repurchased property's cost base, so it
    resurfaces on a genuine later disposition; the pot-level proportional
    ACB accounting makes this exactly restore the pre-sale unrealized-loss
    gap, so the ACB<=FMV invariant is preserved by construction). Pended
    dollars are held in ``ws.new_superficial_loss_pending`` and resolve in
    the next step (denied there if that step repurchases, released into
    that step's settlement otherwise).

    ``ws.superficial_loss_position_adjustment`` carries the net correction
    the ``capital_loss`` rule must fold into the year's signed position:
    +(denied + pended) removes losses the raw ws fields still embed,
    -(released) adds a prior step's loss back in, one step late.

    DP#32: with no realized loss and no pending entries the rule writes
    0.0/[] and returns False -- a strict no-op, so the golden invariant is
    unchanged by construction.

    Returns True when the rule denied, pended, or released anything this
    step (an observable effect), False otherwise.
    """
    from superficial_loss import classify_window

    realized_loss_raw = max(
        0.0, -(ws.solvency_realized_gain + ws.sm_unwind_realized_gain))
    repurchase = (ws.non_reg_alloc
                  + ws.principal_sale_proceeds_invested
                  + ws.sm_readvanced)
    substitutes_declared = bool(ctx.config.superficial_loss_substitute_pairs)

    denied, pended, released, new_pending = classify_window(
        realized_loss_raw=realized_loss_raw,
        repurchase_this_step=repurchase,
        substitutes_declared=substitutes_declared,
        opening_pending=ws.opening_superficial_loss_pending,
        year=ctx.calendar_year,
    )

    ws.new_superficial_loss_pending = new_pending
    ws.superficial_loss_denied = denied
    ws.superficial_loss_acb_added = denied  # s.53(1)(f) deferral
    ws.superficial_loss_pended = pended
    ws.superficial_loss_released = released
    ws.superficial_loss_position_adjustment = denied + pended - released
    if denied > 0.0:
        ws.new_nonreg_acb += denied

    return denied > 0.0 or pended > 0.0 or released > 0.0
