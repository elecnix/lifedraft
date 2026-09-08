"""Issue #140: the capital-loss carry-forward ledger.

The taxable path floors a realized capital LOSS at zero
(``liquidation_waterfall.capital_gains_cost`` floors the taxable gain), so a
loss realized below ACB evaporates instead of sheltering a later gain. These
tests pin the ledger: a net capital loss is recorded in taxable-basis
(included) dollars, offsets capital gains -- same year or carried forward --
and NEVER deducts against ordinary income.

Carryback is deliberately out of scope: this is a forward-only projection.
"""

import os
import sys
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capital_loss_carryforward import settle_year
import rules_capital_loss  # noqa: F401  (registers the `capital_loss` rule)
from rule_registry import RULES, RuleContext, YearWorkingState

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


class TestCapitalLossRule(unittest.TestCase):
    """The registered `capital_loss` rule: settle the year's signed net
    capital position against the pool, net of what the pricing layer
    already sheltered (consumed exactly once). Driven directly, as the
    epic-#795 bite tests drive their registered rules."""

    @staticmethod
    def _ctx(inclusion=INCLUSION):
        from simulation_config import SimulationConfig
        cfg = SimulationConfig.from_dict({
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'inflation': 0.02, 'horizon_age': 95},
            'property': {'house_value': 500000, 'mortgage_balance': 0,
                         'margin_available': 0, 'ltv_max': 0.80,
                         'amortization_years': 25, 'mortgage_rate': 0.045},
            'family': {'members': [{'role': 'primary', 'id': 'p',
                                    'birth_date': '1976-01-01'}],
                       'children': []},
            'accounts': {},
            'tax': {'province': 'qc'},
        })
        return RuleContext(
            year=0, calendar_year=2026, allocations={}, config=cfg,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
            fhsa_contribution=0.0, rrsp_annual_limit=None,
            tfsa_annual_limit=None, fhsa_annual_limit=None,
            non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
            pension_income=0.0, drawdown_order=None,
            rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
            drawdown_net_target=0.0, retiree_marginal_rate=0.0,
            drawdown_bracket_target=None, drawdown_other_taxable_income=0.0)

    def test_loss_year_grows_the_pool(self):
        # A below-ACB forced liquidation (solvency_realized_gain, signed)
        # settles into the pool: $40k raw loss -> $20k includable added.
        ws = YearWorkingState(year=0)
        ws.solvency_realized_gain = -40_000.0
        ws.opening_capital_loss_carryforward = 0.0
        fired = RULES['capital_loss'](ws, self._ctx())
        assert ws.new_capital_loss_carryforward == pytest.approx(20_000.0)
        assert ws.capital_loss_offset_applied == 0.0
        assert fired is True

    def test_pricing_consumed_slice_is_settled_once(self):
        # The drawdown's lead tax-free slice already consumed $6k of the
        # $10k pool against a $20k raw drawdown gain ($10k includable).
        # Both the pool and the position enter settle_year net of the
        # sheltered slice: pool 4k, position 20k - 12k = 8k raw ($4k
        # includable) -> the remaining pool exactly exhausts against it.
        ws = YearWorkingState(year=0)
        ws.drawdown_realized_capital_gain = 20_000.0
        ws.opening_capital_loss_carryforward = 10_000.0
        ws.cg_loss_offset_used = 6_000.0
        fired = RULES['capital_loss'](ws, self._ctx())
        assert ws.new_capital_loss_carryforward == pytest.approx(0.0)
        assert ws.capital_loss_offset_applied == pytest.approx(4_000.0)
        assert fired is True

    def test_empty_pool_no_dispositions_is_a_strict_noop(self):
        # The golden path: nothing realized, nothing carried -> 0.0/0.0,
        # rule reports not-fired (DP#32: a strict no-op, byte-identical).
        ws = YearWorkingState(year=0)
        fired = RULES['capital_loss'](ws, self._ctx())
        assert ws.new_capital_loss_carryforward == 0.0
        assert ws.capital_loss_offset_applied == 0.0
        assert fired is False


class TestDrawdownPricingSeam(unittest.TestCase):
    """The carry-forward pool's cash value: the non-reg draw's lead tax-free
    slice in plan_drawdown_net (cg_loss_offset)."""

    def test_lead_slice_delivered_tax_free(self):
        from countries.canada.retirement_transition import plan_drawdown_net
        # Non-reg pot at 100k with a 50k ACB (gain_frac 0.5); inclusion
        # 0.5 -> every gross dollar is 0.25 taxable. A $10k net need from a
        # $5k includable offset: the whole draw lands in the lead tax-free
        # slice -- nothing recognized as taxable income, and the consumed
        # offset is exactly the draw's taxable slice.
        canada = {'tfsa_primary_balance': 0}
        plan = plan_drawdown_net(
            10_000, ['non_reg'], canada, non_reg_balance=100_000,
            non_reg_acb=50_000, marginal_rate=0.40,
            cg_loss_offset=5_000.0)
        assert plan.net_delivered == pytest.approx(10_000.0)
        assert plan.total_withdrawn == pytest.approx(10_000.0)
        assert plan.taxable_withdrawn == pytest.approx(0.0)
        assert plan.cg_loss_offset_used == pytest.approx(2_500.0)
        # The gain is still REALIZED (sheltering is a tax effect, not an
        # erasure): 10k gross x 0.5 gain_frac.
        assert plan.realized_capital_gain == pytest.approx(5_000.0)

    def test_offset_capped_at_the_draws_taxable_slice(self):
        from countries.canada.retirement_transition import plan_drawdown_net
        # A larger pool than the draw needs: only the draw's taxable slice
        # is sheltered; the unused offset is NOT consumed.
        canada = {'tfsa_primary_balance': 0}
        plan = plan_drawdown_net(
            10_000, ['non_reg'], canada, non_reg_balance=100_000,
            non_reg_acb=50_000, marginal_rate=0.40,
            cg_loss_offset=50_000.0)
        assert plan.net_delivered == pytest.approx(10_000.0)
        assert plan.taxable_withdrawn == pytest.approx(0.0)
        assert plan.cg_loss_offset_used == pytest.approx(2_500.0)

    def test_partial_shelter(self):
        from countries.canada.retirement_transition import plan_drawdown_net
        # A $1k includable offset shelters the FIRST $1k of the draw's
        # taxable slice; the rest is priced at the flat rate.
        canada = {'tfsa_primary_balance': 0}
        plan = plan_drawdown_net(
            10_000, ['non_reg'], canada, non_reg_balance=100_000,
            non_reg_acb=50_000, marginal_rate=0.40,
            cg_loss_offset=1_000.0)
        # Lead slice: 1k/0.25 = 4k gross delivers 4k tax-free. Remaining
        # 6k net at 1 - 0.25*0.40 = 0.90 net per gross -> 6,666.67 gross.
        assert plan.cg_loss_offset_used == pytest.approx(1_000.0)
        assert plan.total_withdrawn == pytest.approx(4_000.0 + 20_000.0 / 3.0)
        assert plan.taxable_withdrawn == pytest.approx(20_000.0 / 3.0 * 0.25)
        assert plan.net_delivered == pytest.approx(10_000.0)

    def test_no_offset_is_byte_identical_to_pre_140(self):
        from countries.canada.retirement_transition import plan_drawdown_net
        canada = {'tfsa_primary_balance': 0}
        baseline = plan_drawdown_net(
            10_000, ['non_reg'], canada, non_reg_balance=100_000,
            non_reg_acb=50_000, marginal_rate=0.40)
        explicit_zero = plan_drawdown_net(
            10_000, ['non_reg'], canada, non_reg_balance=100_000,
            non_reg_acb=50_000, marginal_rate=0.40, cg_loss_offset=0.0)
        assert baseline.total_withdrawn == explicit_zero.total_withdrawn
        assert baseline.taxable_withdrawn == explicit_zero.taxable_withdrawn
        assert baseline.net_delivered == explicit_zero.net_delivered
        assert explicit_zero.cg_loss_offset_used == 0.0

