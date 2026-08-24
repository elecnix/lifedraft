"""The ``superficial_loss`` rule: ITA s.53(1)(c)/s.54 — issue #141.

Applies the superficial-loss statute to the household's DECLARED loss-bearing
dispositions (``people[].superficial_losses[]``, read through
``SimulationConfig.superficial_loss_events()``): a disposition whose
substituted property was acquired within the 30-day window (by the seller OR
an affiliated person -- in this engine, the spouse) and still held 30 days
after the sale has its loss DENIED. The denial is priced two ways:

* the denied slice NEVER joins the year's net capital position, so the ITA
  s.111(1)(b) carry-forward pool (#140) never sees it -- the
  `capital_loss_carryforward` rule adds ``ws.superficial_loss_denied`` back
  out of the signed sources when it nets the year;
* the denied slice IS added to the substituted property's ACB (s.53(1)(f)),
  booked onto the non-registered pot's aggregate cost basis
  (``ws.new_nonreg_acb``) -- the deferred benefit resurfaces as a smaller
  taxable gain when that property is eventually disposed of.

Runs AFTER 'solvency' (whose forced liquidations are the loss sources it
validates against) and BEFORE 'capital_loss_carryforward' (which must net
the DENIAL out of the position it reconciles). One module per government
program (DP#10); the pure s.54 arithmetic lives in ``superficial_loss.py``
(DP#3) and this rule is the wiring.

The engine holds no per-lot acquisition ledger and steps a YEAR at a time,
so the window is DECLARABLE, never auto-detected -- that step-scale
abstraction is disclosed on every run this rule activates (model_fidelity,
issue #141 entry). A household declaring nothing is a strict no-op:
the rule returns False without touching any state (DP#32 -- the golden
path is byte-identical by construction).
"""

from __future__ import annotations

from rule_registry import RuleContext, YearWorkingState, rule

#: Tolerance on the validation that a household cannot deny more loss than
#: it actually realized (floats arrive from waterfall pricing).
_TOL = 1e-6


def _year_gross_realized_losses(ws: YearWorkingState) -> float:
    """The year's GROSS realized capital losses (pre-inclusion magnitude),
    across exactly the sources the `capital_loss_carryforward` rule nets --
    the same list, so a denial can never exceed what the ledger actually
    booked (the two rules validate against one spelling of reality, DP#9).
    """
    signed = [
        ws.drawdown_realized_capital_gain,   # signed (#754)
        ws.solvency_realized_loss,           # signed: the waterfall's loss side (#679)
        ws.heloc_servicing_realized_gain,    # signed (#110/#155)
        ws.sm_unwind_realized_gain,          # signed
        ws.sale_realized_gain,               # signed as of #140
        ws.principal_sale_realized_gain,     # floored at 0 BY STATUTE s.40(2)(b)
    ]
    return -sum(min(0.0, g) for g in signed)


@rule('superficial_loss')
def apply_superficial_loss(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Issue #141: deny the declared superficial losses realized this year.

    For every declared event dated THIS calendar year: the pure s.54 test
    decides superficiality (both window sides and the holding condition are
    priced -- an event outside the window or sold-out-before-day-30 denies
    nothing). Two loud refusals (DP#32):

    * a declared event whose seller/acquirer pair does not resolve inside
      this config's members -- the contract loader refuses at load; this
      runtime guard covers directly-authored configs;
    * declarations totalling MORE than the year's gross realized capital
      losses -- a loss the household did not realize cannot be denied.

    The total denial lands on ``ws.superficial_loss_denied`` (surfaced on
    YearResult) and on ``ws.new_nonreg_acb`` (s.53(1)(f)). Returns True when
    any event was processed for this year; False for a no-declaration year.
    """
    events = [e for e in ctx.config.superficial_loss_events()
              if e.get('year') == ctx.calendar_year]
    if not events:
        return False

    member_ids = {m.get('id', m.get('role'))
                  for m in ctx.config.family_members}
    from superficial_loss import denied_loss

    total_declared = 0.0
    total_denied = 0.0
    for e in events:
        seller = e.get('seller')
        acquirer = e.get('acquired_by')
        if acquirer not in member_ids or seller not in member_ids:
            raise ValueError(
                f"superficial-loss declaration for {ctx.calendar_year} "
                f"references a non-member (seller={seller!r}, "
                f"acquired_by={acquirer!r}); the s.54 affiliated-person "
                f"attribution spans only simulated members (DP#32)")
        amount = float(e['loss_amount'])
        if amount <= 0.0:
            raise ValueError(
                f"superficial-loss declaration for {ctx.calendar_year} has "
                f"loss_amount {amount!r}; there is nothing to deny below or "
                f"at zero (DP#32)")
        total_declared += amount
        total_denied += denied_loss(
            amount,
            int(e['days_to_acquisition']),
            bool(e['still_held_30_days_after']),
        )

    gross_losses = _year_gross_realized_losses(ws)
    if total_declared > gross_losses + _TOL:
        raise ValueError(
            f"household declares {total_declared:.2f} of superficial-loss "
            f"dispositions in {ctx.calendar_year} but only realized "
            f"{gross_losses:.2f} of gross capital losses that year -- a "
            f"loss that was never realized cannot be denied (DP#32)")

    ws.superficial_loss_denied = total_denied
    if total_denied > 0.0:
        # s.53(1)(f): the denied loss is ADDED TO THE ACB of the substituted
        # property -- here, the non-registered pot's aggregate cost basis.
        # No cash moves and no balance changes: this defers tax, it does not
        # create or destroy assets.
        ws.new_nonreg_acb += total_denied
    return True
