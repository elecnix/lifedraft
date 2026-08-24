#!/usr/bin/env python3
"""Tests for issue #936: model a bank DEPOSIT PRODUCT as an optimizer-swept
take/leave option via ``decisions.deposit_products[]``.

One generic mechanism expresses a plain HISA, a term/GIC and a promotional
teaser -- they are different ``rate_schedule``/``rate_eligible_cap`` field
values, not different concepts. The feature makes such a product PURE CONTRACT
DATA the optimizer ranks take-vs-leave, with no per-product code change. The
capabilities under test:

  #1 the yield is ordinary INTEREST -- 100% taxable at the marginal rate each
     year as it accrues, not a deferred/50%-inclusion capital return;
  #2 the rate is a generic ordered STEP schedule walked by elapsed time since
     funding (a flat rate, a dated teaser->base step-down, or a fixed term all
     fall out of the same walk);
  #3 an optional rate_eligible_cap -- the current step's rate applies only up
     to the cap, the excess earns the ongoing (final-step) rate;
  #4 TAKE/LEAVE is an optimizer choice -- each declared product + the implicit
     "leave it" baseline is a ranked candidate;
  #5 funding via a money-conserving year-0 transfer that DEBITS funding_source.

Absence is a strict NO-OP (``TestAbsenceIsNoOp``): a household that declares no
product is byte-identical to today, and the golden invariant does not move.

All test data uses fabricated round numbers (DP#13/DP#15).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import (
    SimulationConfig, ScenarioOverlay, apply_overlay,
)
from simulation_state import SimState, simulate_year_pure
from rule_registry import RULES, RuleContext, YearWorkingState
from simulation_rules import RULE_ORDER
from rules_growth import (
    apply_deposit_product_growth,
    _deposit_product_after_tax_rate,
    _deposit_rate_at,
)
from scenario_discovery import discover_anchors
from simulate import enumerate_overlays
import contract_schema


def _load_example_doc():
    """The shipped contract example, trimmed to the two-generation subset the
    adapter maps (same helper tests/test_input_contract.py / test_issue_823
    use -- the 4-generation full doc is refused by the N-adult uncap)."""
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]

    def _owner_id(acc):
        o = acc.get("owner")
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            return o.get("person")
        return None

    doc["accounts"] = [a for a in doc["accounts"] if _owner_id(a) in keep]
    return doc


def _product(**overrides):
    """A fabricated PROMO deposit product (a teaser step then a base step),
    the richest case -- the flat HISA and term/GIC helpers below drop fields
    from the SAME shape to prove genericity (DP#13/DP#15)."""
    product = {
        'id': 'promo_hisa',
        'label': 'Promo HISA 3% for 730d then 1.5%',
        'account_kind': 'non_reg',
        'fund_amount': 50000.0,
        'funding_source': 'non_reg',
        # generic rate-step schedule: teaser for 730 days (= 2.0 years), then
        # an open-ended base step (the ongoing rate) held to the horizon.
        'rate_schedule': [
            {'rate': 0.03, 'duration_days': 730},
            {'rate': 0.015},
        ],
        'rate_eligible_cap': 500000.0,
        'tax_character': 'interest',
    }
    product.update(overrides)
    return product


def _flat_hisa(**overrides):
    """The trivial case: a plain HISA -- one open-ended step, no cap."""
    product = {
        'id': 'flat_hisa', 'label': 'Plain HISA 1.5%',
        'account_kind': 'non_reg', 'fund_amount': 50000.0,
        'funding_source': 'non_reg',
        'rate_schedule': [{'rate': 0.015}],
        'tax_character': 'interest',
    }
    product.update(overrides)
    return product


def _term_gic(**overrides):
    """A term/GIC expressed as a single TERMED step (a rate for a duration),
    then the ongoing base -- no GIC-specific lock/penalty code, just the
    generic schedule."""
    product = {
        'id': 'gic_5y', 'label': '5-year GIC 4%',
        'account_kind': 'non_reg', 'fund_amount': 50000.0,
        'funding_source': 'non_reg',
        'rate_schedule': [
            {'rate': 0.04, 'duration_years': 5},
            {'rate': 0.015},
        ],
        'tax_character': 'interest',
    }
    product.update(overrides)
    return product


def _config(**overrides):
    """A minimal SimulationConfig with a non-reg opening balance the deposit
    product can be funded from (DP#13/DP#15)."""
    defaults = dict(
        projection_years=5,
        investment_return=0.07,
        mortgage_balance=0,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 120000, 'birth_year': 1985,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        ],
        children=[],
        portfolio_data={'accounts': {'non_reg': {'balance': 100000.0,
                                                 'cost_basis': 100000.0}}},
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _ctx(config, *, year=0, primary_marginal_rate=0.40,
         non_reg_after_tax_return=None, investment_return=0.07):
    """A RuleContext for direct rule invocation (pattern from
    test_issue_823_ftq.py / test_issue_584_rules_registry.py)."""
    return RuleContext(
        year=year, calendar_year=2026 + year, allocations={}, config=config,
        investment_return=investment_return, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=primary_marginal_rate, spouse_marginal_rate=0.0,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=non_reg_after_tax_return, cpp_income=0.0,
        oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        living_costs=0.0, after_tax_income=0.0,
    )


# ── #2: the generic rate-step schedule walk ─────────────────────────────────

class TestScheduleWalk(unittest.TestCase):
    def test_promo_step_then_base_step_by_elapsed_time(self):
        sched = _product()['rate_schedule']
        # 730 days == 2.0 years: teaser covers elapsed years 0 and 1, base from 2
        self.assertEqual(_deposit_rate_at(sched, 0), 0.03)
        self.assertEqual(_deposit_rate_at(sched, 1), 0.03)
        self.assertEqual(_deposit_rate_at(sched, 2), 0.015)
        self.assertEqual(_deposit_rate_at(sched, 40), 0.015)

    def test_flat_single_open_step_is_constant(self):
        sched = _flat_hisa()['rate_schedule']
        self.assertEqual(_deposit_rate_at(sched, 0), 0.015)
        self.assertEqual(_deposit_rate_at(sched, 10), 0.015)

    def test_term_step_then_base(self):
        sched = _term_gic()['rate_schedule']
        self.assertEqual(_deposit_rate_at(sched, 4), 0.04)   # inside the 5y term
        self.assertEqual(_deposit_rate_at(sched, 5), 0.015)  # term elapsed

    def test_all_termed_no_open_step_holds_last_rate(self):
        # Every step is TERMED (no open-ended final step): once all terms
        # elapse, the walk falls through and the last step's rate holds.
        sched = [{'rate': 0.04, 'duration_years': 5}]
        self.assertEqual(_deposit_rate_at(sched, 3), 0.04)   # inside the only term
        self.assertEqual(_deposit_rate_at(sched, 6), 0.04)   # past it -> last rate holds


# ── #1/#2/#3: the rate reader (schedule + cap + interest tax) ────────────────

class TestRateReader(unittest.TestCase):
    def test_teaser_below_cap_is_step_rate_taxed_as_interest(self):
        # #1 + #2: within the teaser step, below the cap, the gross rate is the
        # step rate, taxed 100% as interest -> rate * (1 - mr).
        r = _deposit_product_after_tax_rate(_product(), 0, 50000, 0.40)
        self.assertAlmostEqual(r, 0.03 * (1 - 0.40))

    def test_after_teaser_step_is_base_rate(self):
        # #2 step-down: once the teaser step elapses the rate drops to the base.
        r = _deposit_product_after_tax_rate(_product(), 2, 50000, 0.40)
        self.assertAlmostEqual(r, 0.015 * (1 - 0.40))

    def test_excess_over_cap_earns_ongoing_rate(self):
        # #3 cap: teaser on the first $500k, ongoing (final-step) on the excess
        # -- a balance-weighted blend, taxed as interest.
        bal = 1000000.0
        r = _deposit_product_after_tax_rate(_product(), 0, bal, 0.0)
        expected_gross = (500000 * 0.03 + 500000 * 0.015) / bal
        self.assertAlmostEqual(r, expected_gross)

    def test_flat_product_no_cap_earns_whole_balance_at_rate(self):
        # the trivial HISA: no cap -> the whole balance earns the flat rate,
        # regardless of size (the cap blend is a capped-product feature only).
        r = _deposit_product_after_tax_rate(_flat_hisa(), 0, 5000000, 0.40)
        self.assertAlmostEqual(r, 0.015 * (1 - 0.40))

    def test_term_gic_single_step_scored_as_its_rate(self):
        # a term/GIC is expressible with no new capability: a single termed step.
        during = _deposit_product_after_tax_rate(_term_gic(), 3, 50000, 0.40)
        after = _deposit_product_after_tax_rate(_term_gic(), 6, 50000, 0.40)
        self.assertAlmostEqual(during, 0.04 * (1 - 0.40))
        self.assertAlmostEqual(after, 0.015 * (1 - 0.40))

    def test_zero_balance_earns_nothing(self):
        self.assertEqual(
            _deposit_product_after_tax_rate(_product(), 0, 0.0, 0.40), 0.0)


# ── the growth rule (#1/#2/#3 through the engine seam) ──────────────────────

class TestGrowthRule(unittest.TestCase):
    def test_taken_product_grows_at_step_rate_after_tax(self):
        cfg = _config(deposit_product=_product())
        ws = YearWorkingState(year=0)
        ws.opening_deposit_product_balance = 50000.0
        ctx = _ctx(cfg, year=0, primary_marginal_rate=0.40)
        fired = apply_deposit_product_growth(ws, ctx)
        self.assertTrue(fired)
        self.assertAlmostEqual(ws.new_deposit_product_balance,
                               50000.0 * (1 + 0.03 * 0.60))

    def test_step_down_to_base_after_teaser(self):
        cfg = _config(deposit_product=_product())
        ws = YearWorkingState(year=3)
        ws.opening_deposit_product_balance = 50000.0
        ctx = _ctx(cfg, year=3, primary_marginal_rate=0.40)
        apply_deposit_product_growth(ws, ctx)
        self.assertAlmostEqual(ws.new_deposit_product_balance,
                               50000.0 * (1 + 0.015 * 0.60))

    def test_no_product_is_noop(self):
        # config.deposit_product is None (the "leave it" baseline / every
        # no-product household): the rule does not fire and the balance is
        # carried through unchanged.
        cfg = _config()  # no deposit_product
        ws = YearWorkingState(year=0)
        ws.opening_deposit_product_balance = 0.0
        ctx = _ctx(cfg)
        fired = apply_deposit_product_growth(ws, ctx)
        self.assertFalse(fired)
        self.assertEqual(ws.new_deposit_product_balance, 0.0)


# ── #5: money conservation (the funding transfer debits funding_source) ─────

class TestMoneyConservation(unittest.TestCase):
    def test_take_conserves_total_and_debits_non_reg(self):
        base = _config()
        taken = _config(deposit_product=_product(fund_amount=40000.0))

        s_leave = SimState.initial(base)
        s_take = SimState.initial(taken)

        # #5: the fund_amount is CARVED OUT of non_reg, not layered on top --
        # total assets are identical, non_reg is debited, and the deposit holds
        # exactly the funded amount.
        self.assertAlmostEqual(s_take.total_assets(), s_leave.total_assets())
        self.assertAlmostEqual(s_take.deposit_product_balance, 40000.0)
        self.assertAlmostEqual(s_take.non_reg_balance,
                               s_leave.non_reg_balance - 40000.0)

    def test_funding_more_than_available_is_clamped(self):
        # cannot move more cash into the deposit than the source holds; the
        # take is partial (a real, reported outcome), and money is conserved.
        taken = _config(deposit_product=_product(fund_amount=1000000.0))
        s = SimState.initial(taken)
        self.assertAlmostEqual(s.deposit_product_balance, 100000.0)
        self.assertAlmostEqual(s.non_reg_balance, 0.0)

    def test_unsupported_funding_source_is_refused(self):
        taken = _config(deposit_product=_product(funding_source='rrsp'))
        with self.assertRaises(ValueError):
            SimState.initial(taken)


# ── #4: take/leave is an optimizer-swept candidate ──────────────────────────

class TestTakeLeaveSweep(unittest.TestCase):
    def _base_cfg(self):
        # A complete internal config (the realistic path): the trimmed example
        # doc with a declared product, mapped through to_internal_config.
        doc = _load_example_doc()
        doc['decisions']['deposit_products'] = [_product()]
        return ic.to_internal_config(doc)

    def test_discovery_surfaces_declared_products(self):
        anchors = discover_anchors(self._base_cfg())
        self.assertEqual([p['id'] for p in anchors['deposit_products']],
                         ['promo_hisa'])

    def test_enumerate_produces_take_and_leave_candidates(self):
        overlays = enumerate_overlays(self._base_cfg())
        taken = [o for o in overlays if o.deposit_product is not None]
        left = [o for o in overlays if o.deposit_product is None]
        # both the take-it candidate and the leave-it baseline are enumerated
        self.assertTrue(taken, "expected at least one 'take the product' overlay")
        self.assertTrue(left, "expected at least one 'leave it' baseline overlay")
        self.assertEqual(taken[0].deposit_product['id'], 'promo_hisa')

    def test_apply_overlay_lands_product_on_engine_key(self):
        # DP#18: the taken product must reach the key the engine reads.
        base = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000}]},
            'property': {'house_value': 500000, 'mortgage_balance': 0},
        }
        overlay = ScenarioOverlay(label='take', deposit_product=_product())
        cfg = apply_overlay(base, overlay)
        self.assertEqual(cfg['deposit_product']['id'], 'promo_hisa')
        # and SimulationConfig.from_dict reads it back onto the engine field
        self.assertEqual(SimulationConfig.from_dict(cfg).deposit_product['id'],
                         'promo_hisa')


# ── #4 SCORING: the optimizer ranks take-vs-leave correctly ─────────────────

class TestRankingIsScored(unittest.TestCase):
    """The product must be correctly SCORED: it beats idle cash and loses to a
    higher market return -- the correct behaviour under the deterministic
    engine (a 3% deposit ranks below investing at ~7% and above idle cash)."""

    def _terminal_total(self, *, take, non_reg_after_tax):
        product = _product(fund_amount=50000.0)
        cfg = _config(projection_years=3,
                      deposit_product=product if take else None)
        state = SimState.initial(cfg)
        for year in range(3):
            _result, state = simulate_year_pure(
                state, year=year, allocations={}, config=cfg,
                investment_return=cfg.investment_return,
                primary_marginal_rate=0.40,
                non_reg_after_tax_return=non_reg_after_tax,
            )
        return state.total_assets()

    def test_product_beats_idle_cash(self):
        # In an "idle cash" world (the funding source earns ~0), taking the 3%
        # teaser grows the parked money while leaving it idle does not -> take
        # ranks ABOVE leave.
        take = self._terminal_total(take=True, non_reg_after_tax=0.0)
        leave = self._terminal_total(take=False, non_reg_after_tax=0.0)
        self.assertGreater(take, leave)

    def test_product_loses_to_higher_market_return(self):
        # When the funding source would instead earn a 7% market return, the
        # 3% teaser is the worse home for the money -> leave ranks ABOVE take.
        take = self._terminal_total(take=True, non_reg_after_tax=0.07)
        leave = self._terminal_total(take=False, non_reg_after_tax=0.07)
        self.assertGreater(leave, take)


# ── contract mapping: decisions.deposit_products[] reaches the config ─────────

class TestContractMapping(unittest.TestCase):
    def test_declared_products_reach_internal_config(self):
        doc = _load_example_doc()
        doc['decisions']['deposit_products'] = [_product()]
        legacy = ic.to_internal_config(doc)
        self.assertEqual([p['id'] for p in legacy['deposit_products']],
                         ['promo_hisa'])

    def test_flat_and_term_products_validate_and_map(self):
        # genericity end-to-end: a plain HISA and a term/GIC are the SAME
        # contract shape (only field values differ) and both reach the config.
        doc = _load_example_doc()
        doc['decisions']['deposit_products'] = [_flat_hisa(), _term_gic()]
        legacy = ic.to_internal_config(doc)
        self.assertEqual([p['id'] for p in legacy['deposit_products']],
                         ['flat_hisa', 'gic_5y'])


# ── absence is a strict no-op ───────────────────────────────────────────────

class TestAbsenceIsNoOp(unittest.TestCase):
    def test_rule_is_registered_and_ordered(self):
        self.assertIn('deposit_product_growth', RULES)
        self.assertIn('deposit_product_growth', RULE_ORDER)

    def test_no_product_config_has_none_and_zero_balance(self):
        cfg = _config()  # no deposit_product declared
        self.assertIsNone(cfg.deposit_product)
        self.assertEqual(cfg.deposit_products, [])
        state = SimState.initial(cfg)
        self.assertEqual(state.deposit_product_balance, 0.0)

    def test_example_doc_maps_without_product(self):
        legacy = ic.to_internal_config(_load_example_doc())
        self.assertNotIn('deposit_products', legacy)

    def test_golden_invariant_unchanged(self):
        # The whole feature is additive and absence-safe: the 46-year golden
        # household terminal total_assets must be byte-identical (DP#32).
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        terminal = _run(golden_household_config())[-1].total_assets
        self.assertEqual(terminal, 9709753.139463063)


if __name__ == '__main__':
    unittest.main()
