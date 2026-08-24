"""The ``capital_loss_carryforward`` rule: ITA s.111(1)(b) — issue #140.

Reconciles the year's NET capital position (every signed realized gain/loss
the fold surfaced) against the carried-forward net-capital-loss pool, and
re-persists the pool for next year. Runs after ``solvency`` -- whose forced
liquidations can realize losses -- and before ``amt`` (which reads the raw
realized-gain base and is independent of the loss ledger).

One module per government program (DP#10); the pure arithmetic lives in
``capital_loss_carryforward.py`` (DP#3) and this rule is the wiring.
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule


@rule('capital_loss_carryforward')
def apply_capital_loss_carryforward(ws: YearWorkingState,
                                    ctx: RuleContext) -> bool:
    """Issue #140: advance the ITA s.111(1)(b) net-capital-loss pool one year.

    The year's realized capital gains/losses arrive on ``ws`` from the rules
    that produced them (all PRE-inclusion, 100% basis):

    * ``drawdown_realized_capital_gain`` -- the decumulation's non-reg
      dispositions (signed; #754);
    * ``solvency_realized_gain`` / ``solvency_realized_loss`` -- the forced
      waterfall liquidations, split honestly at zero since #679;
    * ``heloc_servicing_realized_gain`` -- the unfunded-interest servicing
      sales (#110/#155 pricing);
    * ``sm_unwind_realized_gain`` -- the liquidate-to-target SM unwind;
    * ``sale_realized_gain`` -- declared mid-horizon property sales (signed
      as of #140: a sale below ACB is now a realizable capital loss);
    * ``principal_sale_realized_gain`` -- the principal residence's sale
      (floored at 0 BY STATUTE: a principal-residence loss is never an
      allowable capital loss, ITA s.40(2)(b), so it can only contribute 0).

    The drawdown pricing may already have sheltered part of this year's
    taxable gains with the opening pool (``ws.capital_loss_offset_used``,
    written by ``apply_retirement_drawdown``); :func:`advance_pool`
    reconciles both directions:

    * a net-LOSS year joins the whole net loss to the pool (and refunds any
      intra-year pool consumption -- the year's own losses covered its own
      gains first);
    * a net-GAIN year keeps the consumption and carries the remainder.

    Returns True when the year moved anything (a pool existed, was consumed,
    or a net loss was booked); False for an all-zero year (no pool, no
    dispositions -- the golden path), leaving every output at its seeded 0.0
    default: a strict no-op, so the golden invariant is unchanged by
    construction (DP#32).
    """
    from capital_loss_carryforward import advance_pool, year_net_capital_position

    year_net = year_net_capital_position(
        ws.drawdown_realized_capital_gain,
        ws.solvency_realized_gain,
        ws.solvency_realized_loss,
        ws.heloc_servicing_realized_gain,
        ws.sm_unwind_realized_gain,
        ws.sale_realized_gain,
        ws.principal_sale_realized_gain,
    )
    # Issue #141: the `superficial_loss` rule (which runs immediately
    # before this one) DENIED part of the year's realized losses under ITA
    # s.53(1)(c)/s.54 -- a denied loss is never allowable, so it must not
    # join the net position that feeds the s.111(1)(b) pool. Adding the
    # positive denial magnitude back removes exactly that slice from the
    # loss side. 0.0 for every caller that never ran the rule (direct unit
    # tests, households declaring nothing) -- byte-identical (DP#32).
    year_net += ws.superficial_loss_denied
    applied, net_loss_realized, new_pool = advance_pool(
        opening_pool=ws.opening_capital_loss_carryforward,
        offset_used=ws.capital_loss_offset_used,
        year_net_pre_inclusion=year_net,
        inclusion_rate=ctx.config.capital_gains_inclusion,
    )
    ws.capital_loss_applied = applied
    ws.capital_loss_realized = net_loss_realized
    ws.new_capital_loss_carryforward = new_pool
    return bool(applied > 0.0 or net_loss_realized > 0.0
                or new_pool != ws.opening_capital_loss_carryforward)
