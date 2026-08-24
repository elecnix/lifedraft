"""Issue #143: trading friction — the per-transaction cost of turning money over.

The engine prices asset *transitions* as frictionless: every contribution
buys at par, every drawdown/liquidation sells at par, and nothing models the
recurring cost of trading itself (bid/ask spread, commission, ECN fees).
Every strategy comparison that involves different TURNOVER was therefore
biased toward the higher-turnover option by construction — the engine knew
how much a strategy traded and charged nothing for the privilege.

This module owns the ONE spelling of a per-transaction friction model
(DP#3): a DECLARED cost per dollar turned over, applied to the traded
notional of each event the engine actually performs, plus a flat per-event
commission where the engine can honestly COUNT events.

## The honest seam

The engine moves money at POT level (aggregate balances), never per ticket:
there is no order ledger, no per-lot acquisition record, no venue. The only
friction shape that can be priced without inventing machinery the engine
does not have is a proportional cost on the notional each event turns over:

    cost = notional × rebalance_bps / 10_000      (+ count_events × per_trade_fee)

`per_trade_fee` applies ONLY where the event count is real: the year-0 lump
deployment crosses through exactly ONE `fill_room` call, so that seam counts
exactly one trade event. The annual savings deployment prices the bps
spelling only — an aggregate pot's "number of trades" is not observable, and
pricing a fabricated count would be precisely the plausible-wrong-number
defect DP#32 exists to prevent.

## Where it attaches (the trades that actually exist)

Enumerated against this engine, honestly:

1. **Year-0 borrowed lump deployment** (`simulation.py`, both folds): the
   post-#137/#74 effective lump is netted down by its round-trip cost before
   `fill_room`, exactly the seam the deployment carry uses. One countable
   event → the flat fee applies here.
2. **Annual savings deployment**: every simulated year the household's saved
   dollars are allocated across the registered/non-reg pots in one pass.
   That base IS the traded notional, so the cost is ``base × rebalance_bps``
   and only the post-friction remainder reaches the pots — the drag then
   compounds for the rest of the horizon like any real fee. Aggregate pots
   → bps spelling only.

Deliberately NOT attached (disclosed, not silently dropped):

3. **Sale-side events** (retirement drawdown sizing, solvency forced-
   liquidation waterfall): pricing a spread there means re-sizing the gross
   draw through the progressive-bracketing machinery so the household nets
   its target spend AFTER friction — a materially larger change left as a
   follow-up. Property dispositions already price their own declared
   `selling_costs`; charging spread on top would double-price.
4. **Substitute-switch tracking error** (`swap_tracking_error_bps`): the
   parameter is OWNED here (the one spelling, tested below) but priced by
   NO engine event yet. The #141 superficial-loss rule applies the statute
   to DECLARED transaction facts — it never models the rotation itself, so
   there is no switch event carrying a notional to attach the drift to,
   and the wire schema deliberately does not declare the field yet (a
   schema leaf nothing consumes is a DP#18 dead surface). Wiring lands with
   the harvester feature that creates dated substitute-switch trades.

DP#32 is the spine: a document that declares no `trading_friction` block
produces a frictionless model, every gate reads `is_frictionless`, and the
run is byte-identical to the pre-feature output (the golden invariant does
not move).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

BPS = 10_000

#: The declarable fields of the wire block (`trading_friction`). Anything
#: else in a declared block is a typo and is refused loudly (DP#32), never
#: silently dropped.
_DECLARABLE = ("rebalance_bps", "per_trade_fee")


@dataclass(frozen=True)
class TradingFrictionModel:
    """A declared per-transaction friction model.

    Attributes:
        rebalance_bps: Proportional cost per dollar turned over, in basis
            points (5 bps on a $100,000 tranche = $50). 0 = frictionless.
        per_trade_fee: Flat commission per COUNTED trade event (a $9.95
            ticket charge). Applies only where the engine can count events
            honestly — today, the single year-0 lump deployment.
        swap_tracking_error_bps: Extra proportional cost on a substitute
            switch (#141's rotation leg). OWNED here, consumed by no engine
            event yet — see the module docstring.
    """

    rebalance_bps: float = 0.0
    per_trade_fee: float = 0.0
    swap_tracking_error_bps: float = 0.0

    def __post_init__(self):
        for name in ("rebalance_bps", "per_trade_fee", "swap_tracking_error_bps"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"trading_friction.{name} must be a number, got "
                    f"{value!r} (DP#32: refuse loudly, never coerce)")
            if value < 0:
                raise ValueError(
                    f"trading_friction.{name} must be >= 0 (a negative "
                    f"friction PAYS the household to trade), got {value!r}")

    @property
    def is_frictionless(self) -> bool:
        """True when the model charges nothing anywhere — every engine gate
        reads THIS, so an absent declaration short-circuits to today's
        byte-identical behaviour (DP#32)."""
        return (self.rebalance_bps == 0
                and self.per_trade_fee == 0
                and self.swap_tracking_error_bps == 0)

    @classmethod
    def from_decl(cls, decl: Optional[Dict[str, Any]]) -> 'TradingFrictionModel':
        """Build the model from a declared ``trading_friction`` block.

        Absent/empty block -> the frictionless model (strict no-op, DP#32).
        An unknown key is a typo and raises -- a silently-dropped typo would
        leave the household believing its frictions are priced when they are
        not (the founding-defect shape).
        """
        if not decl:
            return cls()
        unknown = set(decl) - set(_DECLARABLE) - {"swap_tracking_error_bps"}
        if unknown:
            raise ValueError(
                f"Unknown key(s) in trading_friction: {sorted(unknown)}. "
                f"Declarable fields: {sorted(_DECLARABLE)}")
        return cls(
            rebalance_bps=decl.get("rebalance_bps", 0),
            per_trade_fee=decl.get("per_trade_fee", 0),
            swap_tracking_error_bps=decl.get("swap_tracking_error_bps", 0),
        )


def round_trip_cost(notional: float, model: TradingFrictionModel,
                    *, count_events: int = 0) -> float:
    """What turning ``notional`` dollars over costs under ``model``.

    Proportional spread on the full notional plus ``count_events × `` the
    flat fee. Callers pass a nonzero count ONLY where the engine performed
    that many real, countable trade events (today: the year-0 lump
    deployment's single ``fill_room`` call). A nonpositive notional has
    nothing to charge and returns 0.0 (DP#32: absence is zero, never a
    fabricated fee).
    """
    if notional <= 0:
        return 0.0
    return notional * model.rebalance_bps / BPS + count_events * model.per_trade_fee


def tracking_drift_cost(switch_notional: float,
                        model: TradingFrictionModel) -> float:
    """The substitute-switch drift on ``switch_notional`` dollars rotated.

    THE one spelling of #143's third friction (owned ahead of its consumer):
    over the ~31-day window the harvester holds the correlated-but-not-
    identical substitute, the two legs can diverge; this prices that drift
    as a proportional cost on the rotated notional. No engine event carries
    a switch notional yet (#141 declares statute facts, not rotation
    trades), so nothing calls this today — wiring lands with the harvester
    feature. A nonpositive notional returns 0.0 (DP#32).
    """
    if switch_notional <= 0:
        return 0.0
    return switch_notional * model.swap_tracking_error_bps / BPS
