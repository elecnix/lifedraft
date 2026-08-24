"""Issue #139: one-time transaction costs and credits on a financial event.

The engine prices the *recurring* difference between two strategies to the
dollar (rate spreads compound, per-account MER rides balances, contributions
sweep), but every one-time origination / exit friction was a hardcoded zero:
refinance origination fees, discharge / quittance fees, title insurance,
appraisal, transfer-out fees, transfer-in rebates, and match bonuses were all
invisible to every objective. This module owns the ONE spelling of a one-time
cost / credit leg (DP#3): a signed, dated, one-off balance-sheet delta tied to
the year it fires, spread across `installment_years` when a match is paid over
its installment period rather than as one closing-day lump.

The adapter folds the legs it produces into the engine's existing dated
cash-flow channel (``cfg.cash_flows`` — the same channel the #1075 origination
cash-back rides), so every objective that folds the balance sheet — net
benefit, solvency, estate — sees them with no new engine machinery (DP#8: one
read of one channel, not N bespoke sinks).

DP#32 is the spine: a document that declares no one-time costs produces no
legs, and the run is byte-identical to the pre-feature output (the golden
invariant must not move). ``kind`` makes the direction explicit
(``"cost"`` = the household pays, ``"credit"`` = the household receives),
never collapsed into the sign of an amount, and the declared magnitude is a
non-negative money figure (DP#32: a real $0 cost is declarable, and never
swallowed by an ``or``-fallback).
"""
from __future__ import annotations

from typing import Any, Dict, List


def map_transaction_costs(doc: Dict[str, Any], start_year: int) -> List[Dict[str, Any]]:
    """``transaction_costs[]`` -> the dated one-time cash-flow legs.

    Each declared cost / credit entry becomes one cash-flow leg per calendar
    year it fires:

      - an entry with no ``installment_years`` fires once, in the calendar
        year of its ``date`` — the closing-day lump case;
      - an entry with ``installment_years=N`` is spread evenly into N equal
        ANNUAL legs starting in the fire year — the transfer-rebate case, where
        a credit paid over its installment period is not priced as a single
        year-0 lump.

    Each leg carries ``year`` / ``amount`` / ``tax_treatment`` in the exact
    shape the engine's cash-flow fold already reads (``cf['year'] ==
    sim_year``), so folding these legs into ``cfg.cash_flows`` needs no engine
    change. ``amount`` is signed: positive for a ``credit``, negative for a
    ``cost``. Every leg is NON-TAXABLE cash — a one-time cost and a transfer
    rebate are after-tax balance-sheet movements (the same treatment the
    existing origination cash-back gets); the household declares the net
    after-tax figure.

    DP#32: a document that declares no ``transaction_costs[]`` yields ``[]``
    — the caller appends nothing, and the run is unchanged.
    """
    out: List[Dict[str, Any]] = []
    for entry in doc.get("transaction_costs", []):
        year = int(entry["date"][:4])
        magnitude = entry["amount"]
        legs = entry.get("installment_years", 1)
        sign = 1.0 if entry["kind"] == "credit" else -1.0
        for k, part in enumerate(_cents_legs(magnitude, legs)):
            leg = {
                "year": year + k,
                "amount": part * sign,
                "tax_treatment": "non-taxable",
                "kind": entry["kind"],
                "label": entry["label"],
                "id": entry["id"],
            }
            out.append(leg)
    return out


def _cents_legs(magnitude: float, legs: int) -> List[float]:
    """Split ``magnitude`` into ``legs`` equal whole-cent portions.

    Every leg is an exact-cent multiple and the final leg absorbs the
    remainder, so ``sum(portions) == magnitude`` to the cent — the fold never
    loses or fabricates money to float rounding (DP#32: no silent loss).
    """
    total_cents = round(magnitude * 100)
    base, remainder = divmod(total_cents, legs)
    return [(base + (1 if i < remainder else 0)) / 100.0 for i in range(legs)]