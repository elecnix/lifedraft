"""Net-capital-loss carry-forward accounting (ITA s.111(1)(b)) — issue #140.

Pre-#140 every realized-gain computation either floored the result at zero
(a disposition below ACB became a phantom ``$0`` gain) or -- after #110/#679 --
reported the loss honestly but dropped it on the floor at year-end: a year
with a realized loss and a year without projected identically, because there
was no LOSS ACCOUNT to book it into. Since a realized capital loss could not
be represented as state, none of the law that operates on it could run.

This module is that missing sibling of the ordinary-loss machinery the AMT
already reads (``non_capital_loss_deducted``, ITA s.111(1)): the pure
arithmetic of the NET-CAPITAL-LOSS ledger.

The statute (s.111(1)(b)): a taxpayer's net capital loss for a year (50%
of the year's capital losses, the "allowable capital loss" netted against the
year's taxable capital gains) is deductible against net taxable capital gains
of OTHER years -- carry BACK 3 years, carry FORWARD indefinitely. The engine
projects forward only (there are no historical returns to amend), so the
carry-BACK leg is out of scope here; the carry-FORWARD is indefinite.

UNITS (one spelling, DP#3): the pool is kept in TAXABLE-basis dollars --
``inclusion_rate x`` the pre-inclusion loss -- so it offsets a year's taxable
capital gain (also inclusion-rate-scaled) DOLLAR FOR DOLLAR, exactly how
s.111(1)(b) applies it. Every pre-inclusion figure crossing this module's
boundary is scaled by ``inclusion_rate`` exactly once, here.

Wiring (DP#26): the registered ``capital_loss_carryforward`` rule
(``rules_capital_loss.py``) calls :func:`advance_pool` DEAD LAST in the fold's
realization sequence (after ``solvency``, whose waterfall can realize losses),
reads the opening pool off ``ws.opening_capital_loss_carryforward``
(``jurisdiction_state['canada']['capital_loss_carryforward']``, persisted by
the epilogue like every other cross-year pool), and writes the new pool back.
Within the year, the drawdown pricing consumed part of the pool directly
(``plan_drawdown_net``'s ``cg_loss_offset`` -- sheltered slices never enter
taxable income); that consumption is recorded on ``ws.capital_loss_offset_used``
and reconciled here so the pool is decremented exactly once.

Pure functions only (DP#3): no hidden state, no I/O, deterministic.
"""

from __future__ import annotations


def year_net_capital_position(*signed_realized_gains: float) -> float:
    """The year's NET capital position: the signed sum of every realized
    capital gain/loss the fold surfaced (pre-inclusion, 100% basis).

    Positive -> a net GAIN (taxable, after the year's own losses netted
    against it first, s.111(1)(b)'s within-year netting). Negative -> a net
    CAPITAL LOSS (the allowable amount joins the carry-forward pool).
    """
    return sum(signed_realized_gains)


def advance_pool(opening_pool: float, offset_used: float,
                 year_net_pre_inclusion: float, inclusion_rate: float) -> tuple:
    """Advance the s.111(1)(b) net-capital-loss pool one year.

    Args:
        opening_pool: the carried-forward pool at Jan-1 (TAXABLE basis,
            i.e. already scaled by ``inclusion_rate``). Never negative.
        offset_used: the pool dollars the year's PRICING already consumed --
            ``plan_drawdown_net`` sheltered this much taxable capital gain
            before it reached the bracket walk. Never negative, never more
            than ``opening_pool`` (the caller caps it).
        year_net_pre_inclusion: the year's NET capital position across ALL
            realization sources (100% pre-inclusion basis; see
            :func:`year_net_capital_position`). Negative = a net capital loss.
        inclusion_rate: the capital-gains inclusion rate (e.g. 0.5). Must lie
            in ``(0, 1]`` -- a fabricated rate would silently mis-scale the
            pool (DP#32: refuse loudly, never default).

    Returns:
        ``(applied, net_loss_realized, new_pool)``:

        * ``applied`` -- the taxable-basis pool dollars that sheltered THIS
          year's gains (what the drawdown pricing consumed). A reporting
          figure; the cash effect already happened where it was priced.
        * ``net_loss_realized`` -- the year's net capital LOSS in
          PRE-inclusion dollars (0.0 in a net-gain year). Reporting figure.
        * ``new_pool`` -- the Dec-31 pool (taxable basis), carried forward
          indefinitely.

    Accounting (the one spelling of s.111(1)(b)):

    * NET-LOSS YEAR (``year_net < 0``): the year's own losses net its own
      gains first -- including any gain the pool's ``offset_used`` already
      sheltered -- so the WHOLE net loss joins the pool and any intra-year
      pool consumption is REFUNDED (the year never needed the pool):

        ``new_pool = opening_pool + (-year_net) * inclusion_rate``

      (``opening_pool`` already had ``offset_used`` conceptually removed by
      the pricing; not subtracting it again IS the refund.)
    * NET-GAIN YEAR: the pool consumption stands and the remainder carries:

        ``new_pool = max(0.0, opening_pool - offset_used)``
    """
    if not 0.0 < inclusion_rate <= 1.0:
        raise ValueError(
            f"inclusion_rate must be in (0, 1] to scale the capital-loss "
            f"pool; got {inclusion_rate!r}")
    if opening_pool < 0.0:
        raise ValueError(f"opening capital-loss pool cannot be negative; "
                         f"got {opening_pool!r}")
    if offset_used < 0.0:
        raise ValueError(f"pool offset used cannot be negative; "
                         f"got {offset_used!r}")
    if offset_used > opening_pool:
        raise ValueError(
            f"pool offset used ({offset_used!r}) exceeds the opening pool "
            f"({opening_pool!r}) -- the drawdown pricing must cap its "
            f"consumption at the available pool")

    if year_net_pre_inclusion < 0.0:
        # Net-loss year: own losses covered own gains; refund the pool
        # consumption and add the net loss (scaled to taxable basis ONCE).
        return offset_used, -year_net_pre_inclusion, (
            opening_pool + (-year_net_pre_inclusion) * inclusion_rate)
    return offset_used, 0.0, max(0.0, opening_pool - offset_used)
