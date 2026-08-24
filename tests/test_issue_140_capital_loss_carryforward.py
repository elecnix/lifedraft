#!/usr/bin/env python3
"""Issue #140: realized capital LOSSES are representable, and the ITA
s.111(1)(b) net-capital-loss carry-forward pools them across years.

Pre-#140 every realized-gain computation floored the result at zero (a
disposition below ACB became a phantom $0 gain), so a realized capital loss
could not be REPRESENTED -- and none of the law that operates on it could
run: no loss ledger, no carry-forward, no sheltering of later gains.

This file verifies, layer by layer (DP#11):

1. The PURE accounting (``capital_loss_carryforward.advance_pool``): a
   $100,000 realized loss books a $50,000 taxable-basis pool; a later year's
   $60,000 gain ($30,000 taxable) consumes part of it; a net-loss year joins
   its whole net loss to the pool (refunding any intra-year consumption);
   invalid inputs refuse loudly (DP#32).
2. The SHELTER pricing (``plan_drawdown_net``'s ``cg_loss_offset``): the
   pool absorbs the decumulation draw's LEAD taxable slice before it enters
   taxable income, the gross-up stays exact (the household draws LESS gross,
   never over-sells), and the consumption is reported for reconciliation.
3. The FOLD wiring: an ENGINE-run household whose cottage sells $100,000
   below its ACB books zero disposition tax, carries the $50,000 pool into
   every later year, and a household with no losses stays all-zero (the
   golden no-op, DP#32).

All figures are fabricated round numbers (DP#4/DP#15); no real personal data
enters this repo.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capital_loss_carryforward import (
    advance_pool, year_net_capital_position)
from countries.canada.retirement_transition import plan_drawdown_net

# ── shared fixture constants (fabricated round numbers, DP#4) ──────────────
_INCLUSION = 0.5          # current federal/QC capital-gains inclusion rate


class TestNetCapitalPosition(unittest.TestCase):
    """year_net_capital_position: the signed sum of the year's gains."""

    def test_sum_of_signed_sources(self):
        # +60k drawdown gain, -100k forced-liquidation loss => net -40k.
        self.assertAlmostEqual(year_net_capital_position(
            60_000.0, 0.0, -100_000.0, 5_000.0, -5_000.0), -40_000.0)

    def test_all_zero(self):
        self.assertEqual(year_net_capital_position(0.0, 0.0), 0.0)


class TestAdvancePoolAccounting(unittest.TestCase):
    """The s.111(1)(b) ledger: pool grows on losses, shrinks on use."""

    def test_issue_example_loss_creates_pool(self):
        # A $100,000 realized LOSS (pre-inclusion) books a $50,000
        # taxable-basis pool carried forward indefinitely.
        applied, realized_loss, new_pool = advance_pool(
            opening_pool=0.0, offset_used=0.0,
            year_net_pre_inclusion=-100_000.0, inclusion_rate=_INCLUSION)
        self.assertEqual(applied, 0.0)
        self.assertAlmostEqual(realized_loss, 100_000.0)
        self.assertAlmostEqual(new_pool, 50_000.0)

    def test_gain_year_consumes_only_the_pricing_share(self):
        # Opening pool $50,000 (from a $100k loss); this year's pricing
        # already sheltered $30,000 of taxable gains (the taxable slice of a
        # $60,000 pre-inclusion gain). The remainder carries.
        applied, realized_loss, new_pool = advance_pool(
            opening_pool=50_000.0, offset_used=30_000.0,
            year_net_pre_inclusion=60_000.0, inclusion_rate=_INCLUSION)
        self.assertAlmostEqual(applied, 30_000.0)
        self.assertEqual(realized_loss, 0.0)
        self.assertAlmostEqual(new_pool, 20_000.0)

    def test_offset_larger_than_needed_caps_at_pool(self):
        # Pool nearly exhausted; consumption can never exceed it.
        applied, _, new_pool = advance_pool(
            opening_pool=10_000.0, offset_used=10_000.0,
            year_net_pre_inclusion=80_000.0, inclusion_rate=_INCLUSION)
        self.assertAlmostEqual(new_pool, 0.0)

    def test_negative_offset_used_refuses(self):
        with self.assertRaises(ValueError):
            advance_pool(10_000.0, -1.0, 0.0, _INCLUSION)

    def test_net_loss_year_refunds_intra_year_consumption(self):
        # The drawdown sheltered $30,000 early in the year, but the year's
        # OTHER sources lost $40,000 more: the year nets to a $40k LOSS, so
        # its own losses covered its own gains -- the whole net loss joins
        # the pool (and the intra-year consumption is refunded, since the
        # pool was never needed). s.111(1)(b)'s within-year netting first.
        applied, realized_loss, new_pool = advance_pool(
            opening_pool=50_000.0, offset_used=30_000.0,
            year_net_pre_inclusion=-40_000.0, inclusion_rate=_INCLUSION)
        self.assertAlmostEqual(applied, 30_000.0)      # reporting: what was used
        self.assertAlmostEqual(realized_loss, 40_000.0)
        self.assertAlmostEqual(new_pool, 70_000.0)     # 50k untouched + 40k*0.5

    def test_no_losses_no_dispositions_is_inert(self):
        applied, realized_loss, new_pool = advance_pool(
            0.0, 0.0, 0.0, _INCLUSION)
        self.assertEqual((applied, realized_loss, new_pool), (0.0, 0.0, 0.0))

    def test_invalid_inputs_refuse_loudly(self):
        # DP#32: a fabricated rate or a negative ledger is a build error,
        # never a silently mis-scaled pool.
        with self.assertRaises(ValueError):
            advance_pool(0.0, 0.0, -1_000.0, inclusion_rate=0.0)
        with self.assertRaises(ValueError):
            advance_pool(-1.0, 0.0, 0.0, _INCLUSION)
        with self.assertRaises(ValueError):
            advance_pool(100.0, 200.0, 0.0, _INCLUSION)


class TestDrawdownShelterPricing(unittest.TestCase):
    """``plan_drawdown_net`` shelters the draw's lead taxable slice with the
    pool BEFORE it enters taxable income -- and the gross-up stays exact.

    Fixture: non-reg pot $200,000 FMV / $140,000 ACB (a 30% accrued-gain
    fraction; each gross dollar carries 15% taxable at the 50% inclusion),
    $20,000 other taxable income, two-bracket progressive table.
    """

    BRACKETS = [{'rate': 0.20, 'max': 50_000}, {'rate': 0.30, 'max': None}]

    def _plan(self, net_need=30_000.0, cg_loss_offset=0.0):
        return plan_drawdown_net(
            net_need, ['non_reg'], canada={},
            non_reg_balance=200_000.0, non_reg_acb=140_000.0,
            marginal_rate=0.30, cg_inclusion=_INCLUSION,
            other_taxable_income=20_000.0, brackets=self.BRACKETS,
            cg_loss_offset=cg_loss_offset)

    def test_no_pool_is_byte_identical_to_default(self):
        p_default = self._plan()
        p_zero = self._plan(cg_loss_offset=0.0)
        for field in ('total_withdrawn', 'taxable_withdrawn',
                      'net_delivered', 'realized_capital_gain',
                      'cg_loss_offset_used'):
            self.assertEqual(getattr(p_default, field),
                             getattr(p_zero, field),
                             f'{field} must be identical with and without '
                             f'an explicit 0.0 pool (DP#32 no-op)')

    def test_fully_sheltered_draw_needs_no_gross_up(self):
        # A $40,000 taxable-basis pool dwarfs the draw's whole taxable slice
        # (30,000 x 0.15 = $4,500): the entire draw is effectively tax-free,
        # so gross == net == $30,000 exactly -- the household sells LESS than
        # the untaxed-for baseline (which had to gross up for tax).
        p_no_pool = self._plan()
        p = self._plan(cg_loss_offset=40_000.0)
        self.assertGreater(p_no_pool.total_withdrawn, p.total_withdrawn)
        self.assertAlmostEqual(p.total_withdrawn, 30_000.0)
        self.assertAlmostEqual(p.net_delivered, 30_000.0)
        self.assertAlmostEqual(p.taxable_withdrawn, 0.0)
        self.assertAlmostEqual(p.cg_loss_offset_used, 4_500.0)
        # The RAW realized gain is still reported honestly at 100%.
        self.assertAlmostEqual(p.realized_capital_gain, 9_000.0)

    def test_partially_sheltered_draw_exact_arithmetic(self):
        # A $2,000 taxable-basis pool covers the lead slice
        # (2,000 / 0.15 = $13,333.33 gross, delivering $13,333.33 net); the
        # residual $16,666.67 need is grossed up at the 20% marginal band:
        # 16,666.67 / (1 - 0.15 * 0.20) = $17,182.32.
        p = self._plan(cg_loss_offset=2_000.0)
        lead = 2_000.0 / 0.15
        tail = (30_000.0 - lead) / (1 - 0.15 * 0.20)
        self.assertAlmostEqual(p.total_withdrawn, lead + tail, places=6)
        self.assertAlmostEqual(p.net_delivered, 30_000.0, places=6)
        self.assertAlmostEqual(p.cg_loss_offset_used, 2_000.0, places=6)
        self.assertAlmostEqual(p.taxable_withdrawn,
                               (lead + tail) * 0.15 - 2_000.0, places=6)
        # And the shelter always beats paying tax on the same draw.
        self.assertLess(p.total_withdrawn,
                        self._plan().total_withdrawn)

    def test_shelter_never_enters_taxable_income_stack(self):
        # With a pool covering everything, NO taxable addition is recognized
        # (so OAS-clawback bases and bracket stacks downstream see nothing).
        p = self._plan(net_need=10_000.0, cg_loss_offset=40_000.0)
        self.assertAlmostEqual(p.taxable_by_owner.get('household', 0.0), 0.0)


# ──────────────────────────────────────────────────────────────────────
# Engine fold: a cottage sold BELOW its ACB books a real loss and the
# pool persists across years. Drives FamilySimulation.run() (which also
# enforces the money-conservation invariant suite on every run).
# ──────────────────────────────────────────────────────────────────────

_COTTAGE_VALUE = 500_000
_COTTAGE_ACB = 600_000          # $100,000 BELOW value: a realizable loss
_SALE_YEAR_INDEX = 2            # sale dated 2028, projections start 2026


def _base_doc():
    from test_input_contract import _load_example, _two_generation_subset
    return _two_generation_subset(_load_example())


def _add_underwater_cottage(doc, sale_date="2028-06-30"):
    doc = copy.deepcopy(doc)
    doc["properties"].append({
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": [],
        "sale": {"date": sale_date, "selling_costs": 0},
    })
    return doc


def _run(doc):
    import input_contract as ic
    import contract_schema
    from simulation_config import SimulationConfig
    from simulation import FamilySimulation
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


class TestLossLedgerThroughFold(unittest.TestCase):
    """End-to-end: the fold BOOKS a below-ACB disposition's loss."""

    @classmethod
    def setUpClass(cls):
        cls.results = _run(_add_underwater_cottage(_base_doc()))

    def test_loss_year_books_zero_tax_and_full_loss(self):
        r = self.results[_SALE_YEAR_INDEX]
        # A disposition $100k below ACB books ZERO tax (the tax clamps at
        # its own edge -- a negative tax would be a fabricated refund)...
        self.assertEqual(r.sale_disposition_tax, 0.0)
        # ...but the LOSS is real and booked pre-inclusion...
        self.assertAlmostEqual(r.capital_loss_realized, 100_000.0)
        # ...and half of it lands in the carry-forward pool (s.111(1)(b)).
        self.assertAlmostEqual(r.capital_loss_carryforward, 50_000.0)

    def test_pool_carries_forward_until_drawdown_shelters_gains(self):
        # Every year AFTER the loss opens with the carried pool; each year
        # closes at opening - applied (the only thing that shrinks the pool
        # is the drawdown shelter actually consuming it). This example
        # household retires into taxable non-reg draws around year index 19:
        # the pool shelters those draws' taxable slices until exhausted,
        # then stops at zero forever (it can never go negative).
        for i in range(_SALE_YEAR_INDEX + 1, len(self.results)):
            r = self.results[i]
            self.assertAlmostEqual(
                r.capital_loss_carryforward,
                r.capital_loss_carryforward_opening
                - r.capital_loss_offset_applied, msg=f"year index {i}")
            self.assertGreaterEqual(r.capital_loss_carryforward, 0.0,
                                    msg=f"year index {i}")
        # The shelter genuinely fired in the drawdown years...
        used_any = any(r.capital_loss_offset_applied > 0.0
                       for r in self.results)
        self.assertTrue(used_any,
                        "the pool should have sheltered some of the "
                        "household's later drawdown gains")
        # ...and once exhausted (after the loss year) it stays exhausted.
        seen_zero = False
        for r in self.results[_SALE_YEAR_INDEX:]:
            if seen_zero:
                self.assertEqual(r.capital_loss_carryforward, 0.0)
            elif r.capital_loss_carryforward == 0.0:
                seen_zero = True

    def test_years_before_the_loss_are_clean(self):
        for i in range(_SALE_YEAR_INDEX):
            r = self.results[i]
            self.assertEqual(r.capital_loss_realized, 0.0)
            self.assertEqual(r.capital_loss_carryforward, 0.0)

    def test_no_loss_household_stays_all_zero(self):
        """The golden no-op (DP#32): a household realizing no capital loss
        has the whole ledger at 0.0 in every year."""
        for i, r in enumerate(_run(_base_doc())):
            self.assertEqual(
                (r.capital_loss_carryforward_opening,
                 r.capital_loss_offset_applied, r.capital_loss_realized,
                 r.capital_loss_carryforward), (0.0, 0.0, 0.0, 0.0),
                f"year index {i}: loss ledger must stay all-zero")


if __name__ == '__main__':
    unittest.main()
