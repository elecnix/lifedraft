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
