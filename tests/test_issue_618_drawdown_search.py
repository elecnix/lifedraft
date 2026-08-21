"""Tests for issue #618: search the drawdown order, not just the savings rate.

#580 wired the deemed-disposition estate into the optimizer's objective, but
the recommended strategy did not change, because `discover_strategies` only
ever varied ACCUMULATION levers (rrsp_pct, tfsa_pct, use_smith, deduct_later,
...) -- `retirement.drawdown_order` was a fixed config input every discovered
candidate shared. This file covers the three pieces that close that gap:

  1. `plan_drawdown_net`'s new bracket-filling token ('rrsp_bracket_fill') --
     unit tests against fabricated balances (DP#4/DP#15).
  2. `scenario_discovery.discover_drawdown_orders` -- the search-dimension
     gating (Issue #303's exact pattern: a horizon that never reaches
     retirement must not multiply the search).
  3. `optimize.run_optimization`'s two-pass decumulation search on the #581
     golden household -- the money question the issue asks: does the
     recommended strategy change, and by how much, once drawdown order is
     actually searched? This reproduces the issue's own motivating evidence
     (TFSA-first vs RRSP-meltdown, ~$549k net-to-heirs swing) mechanically,
     rather than asserting a brittle hardcoded snapshot (this repo's own
     convention -- see test_golden_trajectory_581.py's docstring).

Fabricated, round-numbered data only (DP#4/DP#15) -- no personal data.
"""
from tax_data import default_tax_provider
import copy

import pytest

from countries.canada.retirement_transition import (
    plan_drawdown_net, DRAWDOWN_ORDER_CANDIDATES,
)
from tax_calculator import bracket_ceiling
from scenario_discovery import discover_drawdown_orders
from objective import MAX_AFTER_TAX_ESTATE, MAX_NET_BENEFIT, MAX_TERMINAL_WEALTH
from optimize import run_optimization

from test_golden_trajectory_581 import golden_household_config, START_YEAR


# ============================================================================
# 1. plan_drawdown_net — bracket-filling token
# ============================================================================

def _canada_balances(**overrides):
    base = {
        'tfsa_primary_balance': 200_000,
        'tfsa_spouse_balance': 0,
        'rrsp_balance': 1_000_000,
        'spousal_rrsp_balance': 0,
        'spouse_rrsp_balance': 0,
        'lif_balance': 0,
        'lira_balance': 0,
    }
    base.update(overrides)
    return base


class TestBracketFillToken:

    def test_rrsp_draw_capped_at_headroom_then_falls_through_to_tfsa(self):
        """Net need 70,000; headroom to bracket_target is 48,680 (gross, fully
        taxable) -- the RRSP draw must not exceed it, and the remainder must
        be drawn from the next token (TFSA, tax-free)."""
        order = ['rrsp_bracket_fill', 'tfsa', 'non_reg', 'rrsp']
        plan = plan_drawdown_net(
            70_000, order, _canada_balances(), non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.30, bracket_target=108_680, other_taxable_income=60_000,
        )
        assert plan.balance_deltas['rrsp_balance'] == pytest.approx(-48_680.0)
        assert 'tfsa_primary_balance' in plan.balance_deltas
        # Net delivered must still hit the full need (TFSA makes up the rest).
        assert plan.net_delivered == pytest.approx(70_000.0)

    def test_zero_headroom_skips_bracket_fill_entirely(self):
        """Already at/above the bracket target: no RRSP draw against the
        capped token, the whole need falls through to the next source."""
        order = ['rrsp_bracket_fill', 'tfsa']
        plan = plan_drawdown_net(
            50_000, order, _canada_balances(), non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.30, bracket_target=100_000, other_taxable_income=150_000,
        )
        assert 'rrsp_balance' not in plan.balance_deltas
        assert plan.balance_deltas['tfsa_primary_balance'] == pytest.approx(-50_000.0)

    def test_headroom_shared_across_primary_and_spousal_rrsp(self):
        """The bracket ceiling is a HOUSEHOLD figure -- primary + spousal RRSP
        together may not exceed it, even though they are separate balance
        keys under the same 'rrsp_bracket_fill' token."""
        canada = _canada_balances(rrsp_balance=30_000, spousal_rrsp_balance=1_000_000)
        order = ['rrsp_bracket_fill']
        plan = plan_drawdown_net(
            25_000, order, canada, non_reg_balance=0, non_reg_acb=0,
            marginal_rate=0.20, bracket_target=110_000, other_taxable_income=80_000,
        )
        # Headroom = 30,000. Primary RRSP (30,000 available) exhausts it first;
        # spousal RRSP must not be touched once headroom is gone.
        total_taken = -sum(plan.balance_deltas.values())
        assert total_taken == pytest.approx(30_000.0)
        assert plan.balance_deltas.get('spousal_rrsp_balance', 0.0) == 0.0

    def test_oas_inclusive_base_lowers_headroom_by_the_oas(self):
        """Issue #618 / #363 PR 3: OAS is taxable income for bracket purposes
        (received net of the 15% recovery tax, but the brackets still see the
        full taxable OAS), so the bracket-fill ceiling must fill against
        CPP + pension + OAS -- not CPP + pension alone. Passing that OAS-inclusive
        base via ``bracket_fill_base`` shrinks the RRSP headroom by exactly the
        OAS, so the bracket-fill order stops over-drawing by the OAS slice.

        Hand-computed (DP#15/#4 -- fabricated round numbers):
          * CPP + pension                         = 60,000
          * full (taxable) OAS                     = 10,000
          * bracket_fill_base = 60,000 + 10,000    = 70,000
          * explicit bracket_target                = 100,000
          * OLD ceiling (OAS-excluded): 100,000 - 60,000 = 40,000 gross RRSP
          * NEW ceiling (OAS-included): 100,000 - 70,000 = 30,000 gross RRSP
        The RRSP draw is fully taxable and the cap is on GROSS, so the drawn
        RRSP is exactly the headroom; net_need (70,000) far exceeds what either
        cap delivers, so the cap binds in both and the remainder falls through
        to tax-free TFSA (money is conserved -- full net delivered either way).
        """
        order = ['rrsp_bracket_fill', 'tfsa', 'non_reg', 'rrsp']
        common = dict(
            non_reg_balance=0, non_reg_acb=0, marginal_rate=0.30,
            bracket_target=100_000, other_taxable_income=60_000,
        )
        # OLD ceiling: no OAS-inclusive base supplied -> falls back to
        # other_taxable_income (CPP + pension), the pre-#618 behaviour.
        old = plan_drawdown_net(70_000, order, _canada_balances(), **common)
        # NEW ceiling: OAS-inclusive base = 60,000 + 10,000.
        new = plan_drawdown_net(
            70_000, order, _canada_balances(), bracket_fill_base=70_000, **common)

        assert old.balance_deltas['rrsp_balance'] == pytest.approx(-40_000.0)
        assert new.balance_deltas['rrsp_balance'] == pytest.approx(-30_000.0)
        # The reduction is exactly the OAS (10,000): the ceiling no longer fills
        # the room OAS already occupies.
        reduction = (-new.balance_deltas['rrsp_balance']) - (
            -old.balance_deltas['rrsp_balance'])
        assert reduction == pytest.approx(-10_000.0)
        # Money conservation: the full net is still delivered (TFSA makes up the
        # bigger fall-through under the tighter cap).
        assert old.net_delivered == pytest.approx(70_000.0)
        assert new.net_delivered == pytest.approx(70_000.0)

    def test_bracket_fill_base_none_reproduces_oas_excluded_ceiling(self):
        """DP#13: ``bracket_fill_base=None`` (the default) must reproduce the
        pre-#618 OAS-excluded headroom exactly -- callers that do not opt into
        the OAS-inclusive base are unaffected, so the change only ever tightens
        the cap for callers that supply the base."""
        order = ['rrsp_bracket_fill', 'tfsa']
        common = dict(
            non_reg_balance=0, non_reg_acb=0, marginal_rate=0.30,
            bracket_target=100_000, other_taxable_income=60_000,
        )
        implicit = plan_drawdown_net(70_000, order, _canada_balances(), **common)
        explicit_none = plan_drawdown_net(
            70_000, order, _canada_balances(), bracket_fill_base=None, **common)
        # Both draw the OAS-excluded 40,000 headroom.
        assert implicit.balance_deltas['rrsp_balance'] == pytest.approx(-40_000.0)
        assert (explicit_none.balance_deltas['rrsp_balance']
                == implicit.balance_deltas['rrsp_balance'])

    def test_bracket_target_none_disables_cap_backward_compatible(self):
        """Omitting bracket_target must reproduce the pre-#618 behaviour
        exactly (no cap) -- 'rrsp_bracket_fill' with no ceiling behaves like a
        plain 'rrsp' draw."""
        canada = _canada_balances()
        plan_capped_off = plan_drawdown_net(
            70_000, ['rrsp_bracket_fill'], canada, non_reg_balance=0,
            non_reg_acb=0, marginal_rate=0.30,
        )
        plan_plain_rrsp = plan_drawdown_net(
            70_000, ['rrsp'], canada, non_reg_balance=0,
            non_reg_acb=0, marginal_rate=0.30,
        )
        assert plan_capped_off.total_withdrawn == pytest.approx(plan_plain_rrsp.total_withdrawn)
        assert plan_capped_off.net_delivered == pytest.approx(plan_plain_rrsp.net_delivered)


class TestBracketCeiling:
    """tax_calculator.bracket_ceiling — the auto-detect helper."""

    def test_ceiling_is_the_top_of_the_bracket_containing_income(self):
        brackets = default_tax_provider().get_combined_brackets()
        income = 60_000
        ceiling = bracket_ceiling(income, brackets)
        containing = next(b for b in brackets if b['min'] <= income < (b['max'] or float('inf')))
        assert ceiling == containing['max']

    def test_top_unbounded_bracket_has_zero_headroom(self):
        brackets = default_tax_provider().get_combined_brackets()
        top_bracket_min = brackets[-1]['min']
        income = top_bracket_min + 50_000
        assert bracket_ceiling(income, brackets) == income


# ============================================================================
# 2. discover_drawdown_orders — search-dimension gating (Issue #303 pattern)
# ============================================================================

def _short_horizon_cfg():
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1990, 'gross_income': 100_000,
             'retirement_age': 65},
        ]},
        'assumptions': {'start_year': 2026, 'projection_years': 10},
    }


class TestDiscoverDrawdownOrdersGating:

    def test_short_horizon_returns_single_configured_candidate(self):
        """A 10-year accumulation-only horizon never reaches retirement_age
        65 (primary born 1990) -- the dimension must NOT multiply the search."""
        candidates = discover_drawdown_orders(_short_horizon_cfg())
        assert len(candidates) == 1
        assert candidates[0]['id'] == 'configured'

    def test_long_horizon_via_projection_years_returns_full_candidate_set(self):
        cfg = _short_horizon_cfg()
        cfg['assumptions']['projection_years'] = 40  # crosses age 65
        candidates = discover_drawdown_orders(cfg)
        assert candidates == DRAWDOWN_ORDER_CANDIDATES

    def test_long_horizon_via_horizon_age_also_gates_on(self):
        """Regression: a household configured with assumptions.horizon_age
        (the #581 golden household's own shape) must gate the SAME as one
        configured with an explicit projection_years -- horizon_age is the
        source of truth SimulationConfig itself resolves through
        projection_span(), and the gate must not silently stay closed just
        because it looked at the wrong key."""
        cfg = _short_horizon_cfg()
        del cfg['assumptions']['projection_years']
        cfg['assumptions']['horizon_age'] = 95  # primary born 1990 -> crosses 65
        candidates = discover_drawdown_orders(cfg)
        assert candidates == DRAWDOWN_ORDER_CANDIDATES

    def test_configured_drawdown_order_used_as_fallback_when_gated_off(self):
        cfg = _short_horizon_cfg()
        cfg['retirement'] = {'drawdown_order': ['rrsp', 'tfsa']}
        candidates = discover_drawdown_orders(cfg)
        assert len(candidates) == 1
        assert candidates[0]['order'] == ['rrsp', 'tfsa']


# ============================================================================
# 3. run_optimization — the money question, on the #581 golden household
# ============================================================================

def _accumulation_only_result_count(results):
    """Rows carrying the pass-1 baseline drawdown order."""
    return sum(1 for r in results if r.get('drawdown_order_id') == 'configured')


class TestDrawdownOrderIsSearchedOnGoldenHousehold:

    def test_combinatorics_are_additive_not_multiplicative(self):
        """Guard: results must grow by AT MOST len(DRAWDOWN_ORDER_CANDIDATES)
        extra rows over the pass-1 accumulation count -- never by their
        PRODUCT. A cross-product over 3 accumulation strategies x 4
        decumulation orders would be 12 rows; the two-pass design caps it at
        3 + 4 = 7."""
        cfg = golden_household_config()
        results = run_optimization(cfg, objective=MAX_AFTER_TAX_ESTATE)
        n_accum = _accumulation_only_result_count(results)
        assert n_accum >= 1
        assert n_accum < len(results) <= n_accum + len(DRAWDOWN_ORDER_CANDIDATES)

    def test_short_horizon_run_is_unaffected(self):
        """A household whose horizon never reaches retirement gets back
        EXACTLY the pass-1 accumulation strategies -- pass 2 must cost
        nothing when the gate is closed."""
        cfg = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 150_000, 'birth_year': 1990,
                 'retirement_age': 65,
                 'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 20_000},
                {'role': 'spouse', 'gross_income': 80_000, 'birth_year': 1990,
                 'retirement_age': 65,
                 'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 20_000},
            ]},
            'property': {
                'house_value': 800_000, 'mortgage_balance': 400_000,
                'mortgage_rate': 0.05, 'margin_available': 0,
            },
            'assumptions': {'start_year': 2026, 'projection_years': 10,
                             'investment_return': 0.06, 'salary_growth': 0.02},
        }
        results = run_optimization(cfg, objective=MAX_NET_BENEFIT)
        assert all(r.get('drawdown_order_id') == 'configured' for r in results)

    def test_after_tax_estate_recommendation_beats_pretax_baseline(self):
        """The deliverable: with drawdown order searched AND the after-tax
        estate objective active, the top recommendation must beat the old
        (drawdown-order-blind) recommendation by a material margin -- this is
        the exact $549k swing #618 reports as invisible to the old search
        space, reproduced mechanically rather than hand-computed.
        """
        cfg = golden_household_config()
        results = run_optimization(cfg, objective=MAX_AFTER_TAX_ESTATE)

        # The old (pre-#618) recommendation: best pass-1 (accumulation-only,
        # baseline drawdown order) candidate -- what the optimizer would have
        # returned before this PR.
        baseline_only = [r for r in results if r['drawdown_order_id'] == 'configured']
        old_best = max(baseline_only, key=lambda r: r['objective_score'])

        new_best = max(results, key=lambda r: r['objective_score'])

        # The search must have found something in the decumulation dimension
        # that beats the drawdown-order-blind answer.
        assert new_best['drawdown_order_id'] != 'configured'
        swing = new_best['objective_score'] - old_best['objective_score']
        # Issue #1001 recalibrated this threshold: netting the forced RRIF
        # minimum's after-tax into the discretionary drawdown sizing removed
        # the sub-optimal TFSA over-draw the order search used to correct, so
        # the decumulation-order swing shrank from ~$549k (buggy) to ~$166k.
        # The structural assertion above (the search beats the configured
        # order) still holds; only the magnitude moved with the fix.
        assert swing > 100_000, (
            f"expected a material (>$100k) after-tax-estate improvement from "
            f"searching drawdown order on the golden household; got ${swing:,.0f}"
        )

    def test_pretax_objective_gap_is_smaller_than_after_tax_gap(self):
        """Mechanism check (mirrors test_estate_objective.py's #580 test):
        part of the decumulation win is pre-tax (avoiding RRIF-forced-minimum
        tax drag), but the after-tax objective must see a LARGER gap than the
        pre-tax one -- the residual is the death-tax efficiency #618 exists
        to surface."""
        cfg = golden_household_config()
        results_pretax = run_optimization(cfg, objective=MAX_TERMINAL_WEALTH)
        results_aftertax = run_optimization(cfg, objective=MAX_AFTER_TAX_ESTATE)

        def _gap(results):
            baseline = [r for r in results if r['drawdown_order_id'] == 'configured']
            old_best = max(baseline, key=lambda r: r['objective_score'])
            new_best = max(results, key=lambda r: r['objective_score'])
            return new_best['objective_score'] - old_best['objective_score']

        pretax_gap = _gap(results_pretax)
        aftertax_gap = _gap(results_aftertax)
        assert aftertax_gap > pretax_gap
