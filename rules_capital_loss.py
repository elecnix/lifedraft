"""The ``capital_loss`` rule: the capital-loss carry-forward ledger (issue #140).

A realized capital LOSS must not evaporate: the year's signed net capital
position is settled against the carry-forward pool by
``capital_loss_carryforward.settle_year`` -- same-year losses net against
same-year gains first, the unused includable loss joins the pool, and the
pool shelters a later year's net gain dollar-for-dollar in taxable-basis
dollars. It NEVER deducts against ordinary income (settle_year refuses to
produce an offset against anything but a net capital gain, and the pool's
only pricing consumer -- the retirement drawdown's lead tax-free slice --
shelters the non-reg source's capital-gain slice alone).

Carryback is deliberately out of scope: this is a forward-only projection.

One module per government program (DP#10). Runs AFTER ``solvency`` (the
waterfall's forced liquidations are the last dispositions whose signed
realized gains/losses the position reads) and BEFORE ``amt`` (whose
minimum-amount base must see the NET capital position).
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule


@rule('capital_loss')
def apply_capital_loss(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #140: settle the year's net capital position against the pool.

    The year's SIGNED net capital position (raw dollars: gains positive,
    losses negative) is the sum of every disposition path's realized figure:

    - ``ws.drawdown_realized_capital_gain`` -- the retirement drawdown's
      non-reg disposition (gain_frac floored at 0 by the caller, so >= 0);
    - ``ws.solvency_realized_gain`` -- the forced-liquidation waterfall's
      SIGNED net (unfloored for #140: a below-ACB forced sale realizes a
      genuine loss, and the AMT base and this ledger must both see it);
    - ``ws.sale_realized_gain`` -- declared mid-horizon property sales
      (#956 bite B);
    - ``ws.heloc_servicing_realized_gain`` -- pots sold to service HELOC
      interest (#1034);
    - ``ws.sm_unwind_realized_gain`` -- the liquidate-to-target SM unwind
      (#1017).

    Issue #141: the `superficial_loss` rule (which runs immediately before
    this one) intercepts the security-loss slice of that position under
    ITA s.53(1)(c): losses denied or pended for the annualized window are
    removed from the raw position, and prior-step pended losses released
    this year are added back into it (one step late -- the disclosed
    abstraction). ``ws.superficial_loss_position_adjustment`` carries that
    net correction; 0.0 (a household with no realized security loss) keeps
    the position byte-identical to the pre-#141 settlement.

    The PRINCIPAL residence's pre-apportioned sale figure
    (``ws.principal_sale_realized_gain``, #956 bite E) is deliberately NOT
    part of the position: its exempt fraction is not a capital gain for
    CRA purposes, so the ledger must not consume pool dollars against it
    (the AMT base's separate fold of that figure is that fold's own
    pre-existing choice, not this ledger's).

    What the pricing layer already sheltered is consumed exactly once: the
    retirement drawdown's lead tax-free slice (``ws.cg_loss_offset_used``,
    includable dollars) reduced the pool's buying power at pricing time, so
    both the pool and the position enter ``settle_year`` net of it (the
    sheltered raw gain is ``cg_loss_offset_used / inclusion``).

    DP#25: the pool primitive is imported lazily inside the body. DP#20:
    the inclusion rate is the config-supplied year value, never hardcoded.
    DP#32: with an empty pool and no realized capital gains/losses the rule
    writes 0.0/0.0 and returns False -- a strict no-op, so the golden
    invariant is unchanged by construction.

    Returns True when the pool leaves the year non-empty or an offset was
    applied this year (the rule had an observable effect), False otherwise.
    """
    from capital_loss_carryforward import settle_year

    inclusion = ctx.config.capital_gains_inclusion

    position = (ws.drawdown_realized_capital_gain
                + ws.solvency_realized_gain
                + ws.sale_realized_gain
                + ws.heloc_servicing_realized_gain
                + ws.sm_unwind_realized_gain
                - ws.cg_loss_offset_used / inclusion
                + ws.superficial_loss_position_adjustment)
    pool_net_of_pricing = ws.opening_capital_loss_carryforward - ws.cg_loss_offset_used

    pool_after, offset_applied = settle_year(
        opening_pool=pool_net_of_pricing,
        net_capital_position=position,
        inclusion=inclusion)

    ws.new_capital_loss_carryforward = pool_after
    ws.capital_loss_offset_applied = offset_applied
    return pool_after > 0.0 or offset_applied > 0.0