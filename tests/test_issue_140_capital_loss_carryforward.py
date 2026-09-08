"""Issue #140: the capital-loss carry-forward ledger.

The taxable path floors a realized capital LOSS at zero
(``liquidation_waterfall.capital_gains_cost`` floors the taxable gain), so a
loss realized below ACB evaporates instead of sheltering a later gain. These
tests pin the ledger: a net capital loss is recorded in taxable-basis
(included) dollars, offsets capital gains -- same year or carried forward --
and NEVER deducts against ordinary income.

Carryback is deliberately out of scope: this is a forward-only projection.
"""

import pytest

from capital_loss_carryforward import settle_year


class TestBelowACBDispositionReachesTaxPath:
    def test_below_acb_disposition_loss_is_recorded(self):
        # A forced sale of a pot sitting below its cost basis realizes a
        # signed LOSS at the waterfall seam (#679). Issue #140: that loss
        # must reach the tax path -- recorded into the carry-forward pool
        # in taxable-basis dollars (a $40k raw loss at 50% inclusion is
        # $20k of includable loss).
        pool_after, loss_offset = settle_year(
            opening_pool=0.0, net_capital_position=-40_000.0, inclusion=0.5)
        assert pool_after == pytest.approx(20_000.0)
        # A pure-loss year with an empty opening pool offset nothing.
        assert loss_offset == 0.0
