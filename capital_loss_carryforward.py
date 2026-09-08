"""Issue #140: the capital-loss carry-forward ledger.

A realized capital LOSS (a disposition below ACB) is recorded here in
TAXABLE-BASIS dollars -- the includable fraction -- and carried forward to
shelter a later net capital gain. It NEVER deducts against ordinary income.

Carryback is deliberately omitted: this is a forward-only projection.
The pool lives in ``jurisdiction_state['canada']['capital_loss_carryforward']``.
"""

from __future__ import annotations


def settle_year(opening_pool: float, net_capital_position: float,
                inclusion: float) -> tuple:
    """Settle one year's net capital position against the loss pool.

    Args:
        opening_pool: the carry-forward pool entering the year, in
            taxable-basis (included) dollars. A negative opening pool is
            refused loudly (DP#32) -- the pool only ever grows from losses.
        net_capital_position: the year's SIGNED net capital gain/loss at
            100% (raw dollars): gains positive, losses negative.
        inclusion: the capital-gains inclusion rate for the year (DP#20:
            supplied, never hardcoded).

    Returns:
        ``(pool_after, loss_offset)`` -- the pool leaving the year and the
        taxable-basis loss actually applied against this year's gains.

    Raises:
        ValueError: on a negative opening pool or an inclusion outside
            [0, 1] -- absence and nonsense must fail loudly (DP#32).
    """
    if opening_pool < 0:
        raise ValueError(
            f"capital-loss carry-forward pool is negative ({opening_pool!r}); "
            f"the pool only accumulates losses -- refusing (DP#32)")
    if not 0.0 <= inclusion <= 1.0:
        raise ValueError(
            f"capital-gains inclusion rate out of range: {inclusion!r} (DP#32)")

    position = net_capital_position * inclusion  # taxable basis
    if position >= 0:
        # A net gain year: apply the opening pool dollar-for-dollar, never
        # below zero and never against ordinary income.
        loss_offset = min(opening_pool, position)
        return opening_pool - loss_offset, loss_offset
    # A net loss year: the unused includable loss joins the pool.
    return opening_pool - position, 0.0
