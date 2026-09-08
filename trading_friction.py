"""Issue #143 slice A: the trading-friction cost model (Part 1).

The engine prices asset *transitions* as frictionless: every drawdown and
every forced-liquidation sale delivers ``gross - tax`` and nothing for the
bid/ask spread or the ticket commission the sale actually crosses. Every
optimizer comparison involving different TURNOVER was therefore biased
toward the higher-turnover option by construction -- the engine knew how
much each product traded (the registry's ``turnover`` field) and charged
nothing for the privilege.

This module owns the ONE spelling of a per-transaction friction model
(DP#3): a DECLARED proportional cost per dollar turned over (the bid/ask
spread, in basis points) plus a flat commission per COUNTED trade event.

## The two charging mechanisms, and why they are disjoint

Slice A charges friction at the two trade populations the engine actually
performs, which are DISJOINT by construction:

* **Household redemptions** (Part 2, priced in ``liquidation_waterfall``):
  the sales that move money OUT of the portfolio to fund spending -- the
  solvency forced-liquidation waterfall's non-reg/registered draws. The
  friction is netted out of the proceeds at the same seam the tax already
  is, on the waterfall's GROSS DRAWS. Each waterfall step is one real,
  countable sale event, so the flat commission applies here.
* **Product-internal turnover** (Part 4, priced in ``rules_growth``):
  the manager's own trading to maintain the declared composition -- a
  property of the PRODUCT (the registry's ``turnover``), measured on the
  pot balance, which never moves money out of the household. It lands as
  NAV drag, exactly like #691's MER, through the same ``_blended_pot_rate``
  seam. An aggregate pot's internal trade COUNT is not observable, so the
  annual path prices the spread spelling only -- no commission.

No trade is priced twice: Part 2 never reads ``turnover``, Part 4 never
touches a waterfall draw, and the two bases (waterfall gross draws vs pot
balances) are distinct quantities.

Deliberately NOT attached (disclosed, not silently dropped):

* ``return_model`` rates stay pure gross-return facts -- friction is a cost
  of trading, not a belief about markets, so it does not belong in the rate
  model (the MER/WHT drags subtract in the growth rule, not in the rate).
* The retirement drawdown (``plan_drawdown_net``) and property
  dispositions: property sales already price their own declared
  ``selling_costs`` (charging spread on top would double-price), and the
  retirement drawdown's gross-up machinery is a follow-up-sized change.
* Substitute-switch tracking error (Part 3): depends on issue #141's
  substitute map, which does not exist yet -- out of slice A's scope.

DP#32 is the spine: a document that declares no ``trading_friction`` block
produces a frictionless model, every consumer gates on ``is_frictionless``,
and the run is byte-identical to the pre-feature behaviour (the golden
invariant does not move).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: Basis points per unit of rate: 1 bp = 0.01% = $0.01 per $100.
BPS = 10_000

#: The declarable fields of the wire block (``assumptions.trading_friction``).
#: Anything else in a declared block is a typo and is refused loudly (DP#32),
#: never silently dropped.
_DECLARABLE = ("spread_bps", "commission_per_trade")


@dataclass(frozen=True)
class TradingFrictionModel:
    """A declared per-transaction friction model.

    Attributes:
        spread_bps: The proportional cost of turning one dollar over, in
            basis points -- the bid/ask spread crossed on a round trip plus
            any ad-valorem commission (5 bps on a $100,000 sale = $50). The
            household's DECLARED fact about its venue, never an engine
            assumption. 0 = spread-free.
        commission_per_trade: A flat commission per COUNTED trade event (a
            $9.95 ticket charge). Applies ONLY where the engine performed
            real, countable trade events -- today, each of the solvency
            waterfall's liquidation steps. The annual turnover path passes
            count_events=0 (an aggregate pot's ticket count is not
            observable, and pricing a fabricated count is the
            plausible-wrong-number defect DP#32 exists to prevent).
    """

    spread_bps: float = 0.0
    commission_per_trade: float = 0.0

    def __post_init__(self):
        for name in ("spread_bps", "commission_per_trade"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"trading_friction.{name} must be a number, got "
                    f"{value!r} (DP#32: refuse loudly, never coerce)")
            if value < 0:
                raise ValueError(
                    f"trading_friction.{name} must be >= 0 (a negative "
                    f"friction PAYS the household to trade -- a bad input, "
                    f"not a sign-flipped cost), got {value!r} (DP#32)")

    @property
    def is_frictionless(self) -> bool:
        """True when the model charges nothing anywhere -- every consumer
        gates on THIS, so an absent declaration short-circuits to the
        pre-feature byte-identical behaviour (DP#32)."""
        return self.spread_bps == 0 and self.commission_per_trade == 0

    @classmethod
    def from_decl(cls, decl: Optional[Dict[str, Any]]) -> "TradingFrictionModel":
        """Build the model from a declared ``trading_friction`` block.

        Absent/empty block -> the frictionless model (strict no-op, DP#32).
        An unknown key is a typo and raises -- a silently-dropped typo would
        leave the household believing its frictions are priced when they are
        not (the founding-defect shape).
        """
        if not decl:
            return cls()
        unknown = set(decl) - set(_DECLARABLE)
        if unknown:
            raise ValueError(
                f"Unknown key(s) in trading_friction: {sorted(unknown)}. "
                f"Declarable fields: {sorted(_DECLARABLE)} (DP#32: a typo'd "
                f"key is refused, never silently dropped).")
        return cls(
            spread_bps=decl.get("spread_bps", 0),
            commission_per_trade=decl.get("commission_per_trade", 0),
        )


def transition_cost(notional: float, model: TradingFrictionModel,
                    *, count_events: int = 0) -> float:
    """What trading ``notional`` dollars costs under ``model``.

    Proportional spread on the full notional plus ``count_events`` × the
    flat commission. Callers pass a nonzero count ONLY where the engine
    performed that many real, countable trade events (today: each of the
    solvency waterfall's liquidation steps is one sale). A nonpositive
    notional has nothing to charge and returns 0.0 (DP#32: absence is zero,
    never a fabricated fee). Pure (DP#3).
    """
    if notional <= 0:
        return 0.0
    return (notional * model.spread_bps / BPS
            + max(0, count_events) * model.commission_per_trade)


def annual_turnover_cost(balance: float, turnover: float,
                         model: TradingFrictionModel) -> float:
    """The annual cost of a product-internal turnover of ``turnover`` (a
    fraction, the registry's ``Product.turnover``) on a ``balance`` pot.

    ``balance × turnover`` dollars are turned over each year, at
    ``spread_bps`` per dollar. The flat commission is deliberately NOT
    charged here: the engine moves money at pot level and cannot observe how
    many tickets the product's manager traded, so pricing a fabricated count
    would be a plausible wrong number (DP#32). A nonpositive balance or
    turnover has nothing to charge and returns 0.0 (a 0-turnover product --
    a GIC ladder -- genuinely pays no friction). Pure (DP#3).
    """
    if balance <= 0 or turnover <= 0:
        return 0.0
    return balance * turnover * model.spread_bps / BPS
