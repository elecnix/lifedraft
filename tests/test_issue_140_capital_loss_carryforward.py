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

INCLUSION = 0.5


class TestBelowACBDispositionReachesTaxPath:
    def test_below_acb_disposition_loss_is_recorded(self):
        # A forced sale of a pot sitting below its cost basis realizes a
        # signed LOSS at the waterfall seam (#679). Issue #140: that loss
        # must reach the tax path -- recorded into the carry-forward pool
        # in taxable-basis dollars (a $40k raw loss at 50% inclusion is
        # $20k of includable loss).
        pool_after, loss_offset = settle_year(
            opening_pool=0.0, net_capital_position=-40_000.0, inclusion=INCLUSION)
        assert pool_after == pytest.approx(20_000.0)
        # A pure-loss year with an empty opening pool offset nothing.
        assert loss_offset == 0.0


class TestSameYearOffset:
    def test_loss_offsets_same_year_gain_before_pool_grows(self):
        # (b) A $50k loss and a $20k gain realized in the SAME year net to
        # a $30k raw loss; the $20k gain was offset by the same-year loss
        # (includable: $10k against $10k), so only the EXCESS $30k raw
        # loss ($15k includable) joins the pool. If the whole $50k loss
        # joined the pool instead, the same-year gain would be sheltered
        # twice -- once now and again by the carried pool later.
        net_position = 20_000.0 - 50_000.0
        pool_after, loss_offset = settle_year(
            opening_pool=0.0, net_capital_position=net_position,
            inclusion=INCLUSION)
        assert pool_after == pytest.approx(15_000.0)
        assert loss_offset == 0.0

    def test_pool_shelters_same_year_net_gain(self):
        # (b) the other half: a carried pool offsets THIS year's net gain
        # dollar-for-dollar in taxable-basis dollars, up to the gain.
        pool_after, loss_offset = settle_year(
            opening_pool=10_000.0, net_capital_position=16_000.0,
            inclusion=INCLUSION)
        # $8k includable gain; the $10k pool shelters all of it.
        assert loss_offset == pytest.approx(8_000.0)
        assert pool_after == pytest.approx(2_000.0)

    def test_pool_offset_capped_at_gain(self):
        # The offset never exceeds the year's includable gain: the unused
        # pool remainder survives the year untouched.
        pool_after, loss_offset = settle_year(
            opening_pool=10_000.0, net_capital_position=4_000.0,
            inclusion=INCLUSION)
        assert loss_offset == pytest.approx(2_000.0)
        assert pool_after == pytest.approx(8_000.0)


class TestCarryForward:
    def test_unused_loss_carries_forward_and_shelters_later_gain(self):
        # (c) Year 1: a net loss joins the pool. Year 2: a net gain is
        # sheltered by the carried pool dollar-for-dollar (includable
        # dollars), and only the excess gain is taxed.
        pool_y1, _ = settle_year(
            opening_pool=0.0, net_capital_position=-60_000.0,
            inclusion=INCLUSION)
        assert pool_y1 == pytest.approx(30_000.0)

        # Year 2: a $44k raw gain ($22k includable) against the $30k pool.
        pool_y2, loss_offset = settle_year(
            opening_pool=pool_y1, net_capital_position=44_000.0,
            inclusion=INCLUSION)
        assert loss_offset == pytest.approx(22_000.0)
        assert pool_y2 == pytest.approx(8_000.0)

        # Year 3: the $8k pool remainder shelters the first $8k of the
        # $16k includable gain, then the pool is exhausted.
        pool_y3, loss_offset_y3 = settle_year(
            opening_pool=pool_y2, net_capital_position=32_000.0,
            inclusion=INCLUSION)
        assert loss_offset_y3 == pytest.approx(8_000.0)
        assert pool_y3 == pytest.approx(0.0)


class TestNeverAgainstOrdinaryIncome:
    def test_pool_survives_a_no_gain_year(self):
        # (d) A year with employment income and NO capital transactions
        # (net position 0 -- the ledger sees capital gains only) must not
        # burn the pool: a capital loss never deducts against ordinary
        # income, so the pool carries forward intact.
        pool_after, loss_offset = settle_year(
            opening_pool=25_000.0, net_capital_position=0.0,
            inclusion=INCLUSION)
        assert loss_offset == 0.0
        assert pool_after == pytest.approx(25_000.0)

    def test_loss_year_never_returns_an_offset(self):
        # (d) A net-loss year can never return a positive offset: the loss
        # is recorded, not applied -- and it is not applied against the
        # year's ordinary income either (which settle_year never even
        # receives as an input).
        pool_after, loss_offset = settle_year(
            opening_pool=40_000.0, net_capital_position=-12_000.0,
            inclusion=INCLUSION)
        assert loss_offset == 0.0
        assert pool_after == pytest.approx(46_000.0)

    def test_offset_never_exceeds_capital_position(self):
        # (d) Whatever the pool, the applied offset is bounded by the
        # year's includable CAPITAL gain -- it can never spill onto the
        # ordinary income that sits beside it on the return.
        _, loss_offset = settle_year(
            opening_pool=1_000_000.0, net_capital_position=2_000.0,
            inclusion=INCLUSION)
        assert loss_offset == pytest.approx(1_000.0)

