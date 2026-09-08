#!/usr/bin/env python3
"""Issue #143 slice A: trading friction at the transitions the engine performs.

The engine prices all asset transitions as frictionless: every drawdown and
every forced-liquidation sale delivers ``gross - tax``, with nothing for the
bid/ask spread or the ticket commission the sale actually crosses. Strategies
that differ in turnover are therefore compared UNFAIRLY -- every optimizer
comparison is biased toward the higher-turnover option by construction.

Slice A charges friction at the two places the engine actually trades:

  Part 1 -- the pure cost model (this file's first section): a proportional
  bid/ask spread in basis points per dollar turned over, plus a flat
  commission per COUNTED trade event. Expressed through the existing
  declarable-block + loud-validation mechanisms (the ``transaction_costs``
  precedent), never a parallel path.

  Part 2 -- the solvency forced-liquidation waterfall's SALES: the cost
  functions in ``liquidation_waterfall`` net the friction out of the
  proceeds, exactly as they already net the tax (the seam where "sells at
  par" is true today).

  Part 4 -- the annual turnover drag from the product registry: each pot
  compounds at its rate LESS its holdings' balance-weighted ``turnover``
  times the spread -- the same seam #691's MER and #641's WHT drag use
  (an annual charge on the pot, composed through data, not a parallel path).

The two charging mechanisms are PROVABLY DISJOINT:

  * Part 4 prices the product's INTERNAL turnover -- the manager's own
    trading to maintain the declared composition, a property of the product
    measured on the pot balance, which never moves money out of the
    household (it lands as NAV drag, exactly like MER).
  * Part 2 prices the household's REDEMPTIONS -- sales that move money OUT
    of the portfolio to fund spending, priced on the waterfall's gross
    draws (not on any pot balance, and not on any product's turnover).

A household that both draws down and holds turnover-bearing products is
charged both, but each charge covers a different trade population on a
different base: no dollar is charged twice. Part 2 never reads ``turnover``;
Part 4 never touches a waterfall draw.
"""

import pytest

from trading_friction import (
    BPS,
    TradingFrictionModel,
    annual_turnover_cost,
    transition_cost,
)


# ============================================================================
# Part 1 -- the pure cost model
# ============================================================================


class TestTradingFrictionModel:
    def test_absent_block_is_frictionless(self):
        """An absent/empty declaration is the frictionless model (DP#32:
        absence is a strict no-op, never a coerced zero that pretends to be
        a declared fact)."""
        assert TradingFrictionModel.from_decl(None) == TradingFrictionModel()
        assert TradingFrictionModel.from_decl({}) == TradingFrictionModel()
        assert TradingFrictionModel.from_decl(None).is_frictionless

    def test_declared_block_round_trips(self):
        model = TradingFrictionModel.from_decl(
            {"spread_bps": 5.0, "commission_per_trade": 9.95})
        assert model.spread_bps == 5.0
        assert model.commission_per_trade == 9.95
        assert not model.is_frictionless

    def test_unknown_key_is_refused_loudly(self):
        """A typo'd key is refused, never silently dropped -- a silently
        dropped typo would leave the household believing its frictions are
        priced when they are not (the founding-defect shape, DP#32)."""
        with pytest.raises(ValueError, match="spread_bp"):
            TradingFrictionModel.from_decl({"spread_bp": 5.0})

    def test_negative_spread_is_refused(self):
        """A negative friction PAYS the household to trade -- a bad input,
        not a sign-flipped cost (DP#32)."""
        with pytest.raises(ValueError, match="spread_bps"):
            TradingFrictionModel(spread_bps=-1.0)

    def test_negative_commission_is_refused(self):
        with pytest.raises(ValueError, match="commission_per_trade"):
            TradingFrictionModel(commission_per_trade=-0.01)

    def test_non_numeric_is_refused(self):
        with pytest.raises(ValueError, match="spread_bps"):
            TradingFrictionModel(spread_bps="5")

    def test_declared_zero_is_a_real_value(self):
        """A declared 0 is valid and byte-identical to frictionless (DP#32:
        zero is a value, not a fallback)."""
        assert TradingFrictionModel.from_decl(
            {"spread_bps": 0, "commission_per_trade": 0}).is_frictionless

    def test_is_frozen(self):
        model = TradingFrictionModel(spread_bps=5.0)
        with pytest.raises(Exception):
            model.spread_bps = 6.0


class TestTransitionCost:
    def test_proportional_spread_on_notional(self):
        """5 bps on a $100,000 sale = $50."""
        model = TradingFrictionModel(spread_bps=5.0)
        assert transition_cost(100_000.0, model) == pytest.approx(50.0)

    def test_commission_charged_per_counted_event(self):
        model = TradingFrictionModel(spread_bps=5.0, commission_per_trade=9.95)
        assert transition_cost(100_000.0, model, count_events=2) == pytest.approx(
            100_000.0 * 5.0 / BPS + 2 * 9.95)

    def test_zero_events_charge_no_commission(self):
        """The flat fee applies ONLY where the engine performed real,
        countable trade events -- a caller that cannot count events honestly
        passes count_events=0 and pays only the spread (DP#32: a fabricated
        count is a plausible wrong number)."""
        model = TradingFrictionModel(commission_per_trade=9.95)
        assert transition_cost(100_000.0, model) == 0.0

    def test_nonpositive_notional_costs_nothing(self):
        model = TradingFrictionModel(spread_bps=5.0, commission_per_trade=9.95)
        assert transition_cost(0.0, model, count_events=1) == 0.0
        assert transition_cost(-5.0, model, count_events=1) == 0.0

    def test_frictionless_model_costs_nothing(self):
        assert transition_cost(100_000.0, TradingFrictionModel()) == 0.0


class TestAnnualTurnoverCost:
    def test_turnover_times_balance_times_spread(self):
        """A 0.2-turnover product on a $50,000 pot at 10 bps: the manager
        turns over $10,000 of the pot each year, at $0.10 per $100."""
        model = TradingFrictionModel(spread_bps=10.0)
        assert annual_turnover_cost(50_000.0, 0.2, model) == pytest.approx(
            50_000.0 * 0.2 * 10.0 / BPS)

    def test_zero_turnover_costs_nothing(self):
        """A GIC-ladder product that never trades pays no friction (a real
        zero, not a fallback)."""
        model = TradingFrictionModel(spread_bps=10.0)
        assert annual_turnover_cost(50_000.0, 0.0, model) == 0.0

    def test_zero_balance_costs_nothing(self):
        model = TradingFrictionModel(spread_bps=10.0)
        assert annual_turnover_cost(0.0, 0.2, model) == 0.0

    def test_no_commission_on_the_annual_path(self):
        """The aggregate pot's internal trade COUNT is not observable (the
        engine moves money at pot level, never per ticket), so the annual
        turnover charge prices the spread spelling only -- pricing a
        fabricated count would be precisely the plausible-wrong-number
        defect DP#32 exists to prevent."""
        model = TradingFrictionModel(spread_bps=0.0, commission_per_trade=9.95)
        assert annual_turnover_cost(50_000.0, 0.2, model) == 0.0


# ============================================================================
# Part 2 -- friction at the forced-liquidation waterfall's sales
# ============================================================================

from liquidation_waterfall import (
    LiquidationSource,
    capital_gains_cost,
    identity_cost,
    ordinary_income_cost,
    run_waterfall,
)

SPREAD_MODEL = TradingFrictionModel(spread_bps=5.0)
FULL_MODEL = TradingFrictionModel(spread_bps=5.0, commission_per_trade=9.95)


class TestWaterfallSaleFriction:
    def test_capital_gains_cost_nets_spread_out_of_proceeds(self):
        """The seam where "sells at par" is true today: the sale delivers
        gross - tax - spread. The spread is an additional leakage beside the
        tax, never a second tax."""
        plain = capital_gains_cost(0.4, 0.5, 0.35)
        with_friction = capital_gains_cost(0.4, 0.5, 0.35, friction=SPREAD_MODEL)
        gross = 10_000.0
        net_plain, tax_plain, gain_plain = plain(gross)
        net_f, tax_f, gain_f = with_friction(gross)
        assert tax_f == tax_plain
        assert gain_f == gain_plain
        assert net_f == pytest.approx(net_plain - gross * 5.0 / BPS)

    def test_ordinary_income_cost_nets_spread_out_of_proceeds(self):
        plain = ordinary_income_cost(0.30)
        with_friction = ordinary_income_cost(0.30, friction=SPREAD_MODEL)
        net_plain, _, _ = plain(10_000.0)
        net_f, tax_f, _ = with_friction(10_000.0)
        assert tax_f == pytest.approx(3_000.0)
        assert net_f == pytest.approx(net_plain - 10_000.0 * 5.0 / BPS)

    def test_identity_cost_untouched(self):
        """The reserve is cash and the credit facility is debt -- neither is
        an asset sale, so neither crosses a spread. Friction prices SALES."""
        net, tax, gain = identity_cost(1_000.0, friction=SPREAD_MODEL)
        assert (net, tax, gain) == (1_000.0, 0.0, 0.0)

    def test_no_friction_model_is_byte_identical(self):
        """The default (no friction model) is exactly today's behaviour
        (DP#32: the golden path must not move)."""
        plain = capital_gains_cost(0.4, 0.5, 0.35)
        assert plain(7_777.0) == capital_gains_cost(0.4, 0.5, 0.35)(7_777.0)
        net, tax, gain = capital_gains_cost(0.4, 0.5, 0.35)(10_000.0)
        expected_tax = 0.4 * 0.5 * 0.35 * 10_000.0
        assert (net, tax, gain) == pytest.approx((10_000.0 - expected_tax,
                                                  expected_tax,
                                                  4_000.0))

    def test_step_conservation_gross_equals_net_plus_tax_plus_friction(self):
        """Money conservation at the step: the gross drawn from the source
        balance is fully accounted -- net delivered + tax remitted + friction
        paid to the market. Nothing vanishes, nothing is double-booked."""
        cost = ordinary_income_cost(0.30, friction=SPREAD_MODEL)
        net, tax, _ = cost(10_000.0)
        assert 10_000.0 == pytest.approx(net + tax + 10_000.0 * 5.0 / BPS)


class TestWaterfallCommissionGrossUp:
    def _sources(self, cost):
        return [LiquidationSource('non_reg', 1_000_000.0, cost)]

    def test_affine_cost_fn_grosses_up_exactly(self):
        """A flat commission makes the cost function AFFINE (net = k*gross -
        fee), not linear. The waterfall's gross-up must solve the affine
        equation exactly -- the household still nets its target AFTER
        friction, and the fee leaves the household (it is inside the gross
        drawn, not invented on top)."""
        cost = ordinary_income_cost(0.30, friction=FULL_MODEL)
        shortfall = 10_000.0
        result = run_waterfall(shortfall, self._sources(cost))
        assert not result.ruined
        assert result.covered == pytest.approx(shortfall, abs=1e-6)
        step = result.steps[0]
        k = 1.0 - 0.30 - 5.0 / BPS
        expected_gross = (shortfall + 9.95) / k
        assert step.gross_drawn == pytest.approx(expected_gross, rel=1e-9)
        # Step-level conservation: the gross drawn is fully accounted --
        # net delivered + tax remitted + friction paid to the market.
        assert step.gross_drawn == pytest.approx(
            step.net_proceeds + step.tax + step.friction, abs=1e-6)
        # And the friction is exactly the spread plus the one commission.
        assert step.friction == pytest.approx(
            expected_gross * 5.0 / BPS + 9.95, abs=1e-6)

    def test_linear_cost_fn_unchanged_by_affine_solver(self):
        """Without a commission the affine solve reduces EXACTLY to today's
        single-probe grossing (behaviour-preserving for every existing cost
        function)."""
        cost = ordinary_income_cost(0.30, friction=SPREAD_MODEL)
        result = run_waterfall(10_000.0, self._sources(cost))
        step = result.steps[0]
        assert step.gross_drawn == pytest.approx(10_000.0 / (1.0 - 0.30 - 5.0 / BPS), rel=1e-9)

    def test_frictionless_waterfall_identical_to_plain(self):
        plain = run_waterfall(10_000.0, self._sources(ordinary_income_cost(0.30)))
        frictionless = run_waterfall(10_000.0, self._sources(ordinary_income_cost(0.30)))
        assert plain.steps[0].gross_drawn == frictionless.steps[0].gross_drawn
        assert plain.steps[0].friction == 0.0

    def test_step_records_friction_paid(self):
        cost = ordinary_income_cost(0.30, friction=FULL_MODEL)
        result = run_waterfall(10_000.0, self._sources(cost))
        step = result.steps[0]
        assert step.friction == pytest.approx(step.gross_drawn * 5.0 / BPS + 9.95)


# ============================================================================
# Part 2 wiring -- the DECLARED block reaches the engine fold
# ============================================================================

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
from dataclasses import replace

from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure


def _roundtrip_config(**overrides):
    """A minimal SimulationConfig with fabricated round numbers (DP#13/DP#15)."""
    defaults = dict(
        projection_years=5, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[], mortgage_balance=0, mortgage_rate=0.05,
        house_value=0,
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


class TestDeclaredBlockReachesTheConfig:
    """The declared ``assumptions.trading_friction`` block flows through the
    internal config (DP#24): absent -> None and not re-emitted; declared
    (even all-zero) -> a model that round-trips verbatim."""

    def test_absent_block_is_none_and_not_emitted(self):
        cfg = _roundtrip_config()
        assert cfg.trading_friction is None
        assert 'trading_friction' not in cfg.to_dict()['assumptions']

    def test_declared_block_round_trips(self):
        cfg = replace(_roundtrip_config(), trading_friction=FULL_MODEL)
        d = cfg.to_dict()
        assert d['assumptions']['trading_friction'] == {
            'spread_bps': 5.0, 'commission_per_trade': 9.95}
        cfg2 = SimulationConfig.from_dict(d)
        assert cfg2.trading_friction == FULL_MODEL


class TestSolvencyFoldChargesFriction:
    """The fold's forced-liquidation waterfall (``rules_solvency``) prices the
    declared model on its SALES: each step grosses up so the household still
    nets its target AFTER friction, and the friction leaves the household
    inside the gross draw (the balance drops by the full gross)."""

    def _run(self, trading_friction=None, *, living_costs=50_000.0,
             after_tax_income=58_000.0, non_reg=1_000_000.0):
        """A household whose only liquid asset is a non-reg pot at cost basis
        (no gain -> no tax), facing a $10,000 shortfall (living costs + the
        $18,000 mortgage payment minus $58,000 after-tax income)."""
        cfg = _roundtrip_config(
            investment_return=0.0, mortgage_balance=300_000,
            trading_friction=trading_friction)
        state = SimState(non_reg_balance=non_reg, non_reg_acb=non_reg)
        return simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data={'end_balance': 294_000.0, 'total_payment': 18_000.0,
                           'total_interest': 12_000.0, 'total_principal': 6_000.0},
            living_costs=living_costs, after_tax_income=after_tax_income,
        )

    def test_no_model_is_byte_identical_plain(self):
        result, new_state = self._run()
        event = result.forced_liquidation_events[0]
        assert event['source'] == 'non_reg'
        assert event['gross_drawn'] == pytest.approx(10_000.0, abs=1e-6)
        assert event.get('friction', 0.0) == 0.0
        assert new_state.non_reg_balance == pytest.approx(990_000.0, abs=1e-6)

    def test_spread_grosses_up_and_leaves_inside_the_draw(self):
        result, new_state = self._run(trading_friction=SPREAD_MODEL)
        event = result.forced_liquidation_events[0]
        expected_gross = 10_000.0 / (1.0 - 5.0 / BPS)
        assert event['gross_drawn'] == pytest.approx(expected_gross, rel=1e-9)
        # The household still nets its target AFTER friction -- the shortfall
        # is fully covered, same as the frictionless run.
        assert result.solvency_covered == pytest.approx(10_000.0, abs=1e-6)
        assert not result.ruined
        # Step-level conservation: gross = net + tax + friction.
        assert event['gross_drawn'] == pytest.approx(
            event['net_proceeds'] + event['tax'] + event['friction'], abs=1e-6)
        # The friction LEFT the household: the pot dropped by the full gross,
        # which is the frictionless drop PLUS the friction.
        _, plain_state = self._run()
        assert new_state.non_reg_balance == pytest.approx(
            plain_state.non_reg_balance - event['friction'], abs=1e-6)


# ============================================================================
# Part 4 -- the annual turnover drag from the product registry
# ============================================================================

from rule_registry import RuleContext, YearWorkingState
from rules_growth import _blended_pot_rate, apply_non_reg_growth


def _drag_config(**overrides):
    defaults = dict(
        projection_years=5, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[], mortgage_balance=0, mortgage_rate=0.05,
        house_value=0,
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _drag_ctx(config, *, investment_return=0.07, non_reg_after_tax_return=None):
    return RuleContext(
        year=0, calendar_year=2026, allocations={}, config=config,
        investment_return=investment_return, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.40, spouse_marginal_rate=0.0,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=non_reg_after_tax_return, cpp_income=0.0,
        oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        living_costs=0.0, after_tax_income=0.0,
    )


class TestAnnualTurnoverDrag:
    """Part 4: each pot compounds at its rate LESS its holdings'
    balance-weighted ``turnover`` (the registry's Product.turnover) times the
    declared spread -- the same NAV-drag seam #691's MER and #641's WHT use.
    DISJOINT from Part 2 by construction: this prices the product's INTERNAL
    turnover on the pot BALANCE (money that never leaves the household); Part
    2 prices the household's redemptions on the waterfall's GROSS DRAWS. Part
    4 never reads a waterfall draw; Part 2 never reads ``turnover``."""

    def test_no_friction_model_is_a_no_op_even_with_turnover(self):
        """A household with turnover-bearing products but no declared
        friction model keeps today's rate exactly (DP#32 golden no-op)."""
        cfg = _drag_config(turnover_drag={'rrsp': 0.2})
        assert _blended_pot_rate(_drag_ctx(cfg), 'rrsp', 100_000) == 0.07

    def test_drag_is_turnover_times_spread(self):
        cfg = _drag_config(turnover_drag={'rrsp': 0.2},
                           trading_friction=SPREAD_MODEL)
        # 20% of the pot turns over each year at 5 bps: 0.2 * 0.0005 = 1 bp.
        assert _blended_pot_rate(_drag_ctx(cfg), 'rrsp', 100_000) \
            == pytest.approx(0.07 - 0.2 * 5.0 / BPS)

    def test_drag_is_per_kind(self):
        cfg = _drag_config(turnover_drag={'tfsa': 0.2},
                           trading_friction=SPREAD_MODEL)
        assert _blended_pot_rate(_drag_ctx(cfg), 'rrsp', 100_000) == 0.07

    def test_drag_does_not_decay_with_pot_size(self):
        cfg = _drag_config(turnover_drag={'rrsp': 0.2},
                           trading_friction=SPREAD_MODEL)
        ctx = _drag_ctx(cfg)
        assert _blended_pot_rate(ctx, 'rrsp', 100_000) \
            == _blended_pot_rate(ctx, 'rrsp', 200_000)

    def test_non_reg_dp27_path_drags_once(self):
        """The DP#27 after-tax path bypasses the blended-rate seam, so the
        drag is applied there too -- EXACTLY ONCE (a double subtraction is
        the quiet double-charge this slice exists to prevent)."""
        cfg = _drag_config(turnover_drag={'non_reg': 0.2},
                           trading_friction=SPREAD_MODEL)
        ws = YearWorkingState(new_nonreg_bal=100_000.0)
        apply_non_reg_growth(
            ws, _drag_ctx(cfg, investment_return=0.07,
                          non_reg_after_tax_return=0.05))
        assert ws.non_reg_growth_rate == pytest.approx(0.05 - 0.2 * 5.0 / BPS)
        assert ws.new_nonreg_bal == pytest.approx(100_000.0 * (1.0499))

    def test_non_reg_fallback_path_drags_once(self):
        """The flat-rate fallback path goes through the blended-rate seam,
        which already carries the drag -- again exactly once."""
        cfg = _drag_config(turnover_drag={'non_reg': 0.2},
                           trading_friction=SPREAD_MODEL)
        ws = YearWorkingState(new_nonreg_bal=100_000.0)
        apply_non_reg_growth(ws, _drag_ctx(cfg))
        assert ws.new_nonreg_bal == pytest.approx(100_000.0 * (1.07 - 0.2 * 5.0 / BPS))


class TestTurnoverDragFromTheRegistry:
    """The blended per-pot turnover is derived at the contract boundary from
    the SAME representation the engine already carries -- the accounts'
    ``holdings`` resolved against ``assumptions.products`` -- never a second
    turnover spelling (the wiring is the job; the concept already exists)."""

    def _doc(self, products, accounts):
        return {'accounts': accounts, 'assumptions': {'products': products}}

    def _acc(self, kind, balance, holdings):
        return {'kind': kind, 'balance': {'amount': balance}, 'holdings': holdings}

    def test_single_account_single_product(self):
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc({'p_a': {'category': 'global_equity_index', 'turnover': 0.2}},
                        [self._acc('rrsp', 100_000, [{'product': 'p_a', 'weight': 1.0}])])
        assert _turnover_drag_by_kind(doc, doc['assumptions']['products']) == {'rrsp': 0.2}

    def test_balance_weighted_across_accounts_of_a_kind(self):
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc(
            {'p_slow': {'category': 'global_equity_index', 'turnover': 0.05},
             'p_fast': {'category': 'global_equity_index', 'turnover': 0.45}},
            [self._acc('rrsp', 300_000, [{'product': 'p_slow', 'weight': 1.0}]),
             self._acc('rrsp', 100_000, [{'product': 'p_fast', 'weight': 1.0}])])
        # (300k*0.05 + 100k*0.45) / 400k = 0.15
        drag = _turnover_drag_by_kind(doc, doc['assumptions']['products'])
        assert drag['rrsp'] == pytest.approx(0.15)

    def test_weighted_within_one_account(self):
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc(
            {'p_slow': {'category': 'global_equity_index', 'turnover': 0.05},
             'p_fast': {'category': 'global_equity_index', 'turnover': 0.45}},
            [self._acc('non_reg', 100_000,
                       [{'product': 'p_slow', 'weight': 0.75},
                        {'product': 'p_fast', 'weight': 0.25}])])
        drag = _turnover_drag_by_kind(doc, doc['assumptions']['products'])
        assert drag['non_reg'] == pytest.approx(0.75 * 0.05 + 0.25 * 0.45)

    def test_empty_when_no_holdings(self):
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc({'p_a': {'category': 'global_equity_index', 'turnover': 0.2}}, [])
        assert _turnover_drag_by_kind(doc, doc['assumptions']['products']) == {}

    def test_unknown_product_is_refused_loudly(self):
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc({},
                        [self._acc('rrsp', 100_000, [{'product': 'typo_name', 'weight': 1.0}])])
        with pytest.raises(ValueError, match='typo_name'):
            _turnover_drag_by_kind(doc, doc['assumptions']['products'])

    def test_spousal_rrsp_folds_into_the_rrsp_pot(self):
        """The spousal pot compounds at the rrsp rate (the fold sums both
        balances into one ``rrsp`` rate), so its holdings' turnover prices
        into the rrsp pot -- balance-weighted with the rrsp accounts'."""
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc(
            {'p_slow': {'category': 'global_equity_index', 'turnover': 0.05},
             'p_fast': {'category': 'global_equity_index', 'turnover': 0.45}},
            [self._acc('rrsp', 300_000, [{'product': 'p_slow', 'weight': 1.0}]),
             self._acc('spousal_rrsp', 100_000, [{'product': 'p_fast', 'weight': 1.0}])])
        drag = _turnover_drag_by_kind(doc, doc['assumptions']['products'])
        assert drag == {'rrsp': pytest.approx(0.15)}

    def test_pots_without_a_blended_rate_consumer_are_not_mapped(self):
        """resp/lsif growth does not run through the blended-rate seam, so
        mapping their turnover would be a dead write (DP#18): deliberately
        absent, disclosed in the derivation's docstring."""
        from contract_accounts import _turnover_drag_by_kind
        doc = self._doc({'p_a': {'category': 'global_equity_index', 'turnover': 0.2}},
                        [self._acc('resp', 100_000, [{'product': 'p_a', 'weight': 1.0}]),
                         self._acc('lsif', 50_000, [{'product': 'p_a', 'weight': 1.0}])])
        assert _turnover_drag_by_kind(doc, doc['assumptions']['products']) == {}

    def test_drag_round_trips_through_the_config(self):
        cfg = replace(_roundtrip_config(),
                      turnover_drag={'rrsp': 0.2, 'non_reg': 0.15})
        d = cfg.to_dict()
        assert d['accounts']['turnover_drag'] == {'rrsp': 0.2, 'non_reg': 0.15}
        cfg2 = SimulationConfig.from_dict(d)
        assert cfg2.turnover_drag == {'rrsp': 0.2, 'non_reg': 0.15}
