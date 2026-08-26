"""The ``transaction_costs[]`` adapter: contract document -> internal config.

Issue #139: the general one-time transaction-cost/credit mechanism. The
schema block (``schema/defs/transaction_costs.json``) declares the entries;
this module maps them onto the internal config shape the engine reads:

* a ``refinance_origination`` LUMP (no installments) at year 0 -> the signed
  net year-0 cost written onto ``property.transaction_cost_year0`` (the
  year-0-equivalent deployable-principal reduction seam #137's deployment-
  lag carry uses -- reused, not reinvented). Positive = net cost (reduces
  the deployable principal so NET proceeds deploy); negative = net credit
  (a lender credit adds free money to the advance); 0.0 = none declared
  (byte-identical, DP#32).
* an installment entry (any event) -> N equal monthly payments aggregated
  into calendar years, returned as cash flows the existing ``cash_flows``
  mechanism consumes (an installment is NOT a year-0 lump; it projects to a
  different trajectory than the same amount paid as a lump).
* a non-installment ``account_transfer_in`` entry (fee-out cost,
  reimbursement credit) and a non-year-0 ``refinance_origination`` lump ->
  a single dated cash flow (a cost reduces the year's savings, a credit
  adds to it).

Every entry is validated loudly (DP#32) via
``transaction_costs.validate_transaction_cost``: a negative amount, an
unknown direction, a date before ``as_of``, and a missing date/anchor are
each refused rather than producing a plausible wrong number.

A household that declares NO ``transaction_costs`` maps to a 0.0 net year-0
cost and NO cash flows -- byte-identical to the pre-feature path (DP#32:
absence is absence; the golden household declares none).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import transaction_costs


def map_transaction_costs(
    doc: Dict, as_of: str
) -> Tuple[float, List[Dict[str, Any]]]:
    """``transaction_costs[]`` -> ``(year0_net_refinance_cost, cash_flows)``.

    Args:
        doc: the validated contract document.
        as_of: the document's ``as_of`` date (ISO ``YYYY-MM-DD``).

    Returns:
        A 2-tuple ``(year0_net, cash_flows)``:

        * ``year0_net`` -- the FLOORED net year-0 LUMP refinance-origination
          cost (``max(raw_net, 0.0)``), to be written onto
          ``property.transaction_cost_year0`` and applied as a year-0-
          equivalent reduction of the deployable principal. A net CREDIT is
          floored to 0.0 here so the deployable principal never exceeds the
          borrowed lump (DP#18); the excess credit is routed as a savings
          cash flow instead (see ``cash_flows``). 0.0 when no year-0
          refinance origination lumps are declared or costs offset credits.
        * ``cash_flows`` -- the dated cash flows for installment entries,
          non-year-0-lump entries, AND the excess net refinance credit (a
          lender credit larger than the fees, routed as a year-0 SAVINGS
          cash flow so free principal is not booked against no debt), to be
          appended to the internal ``cash_flows`` list. Empty when none are
          declared.

        Both are 0.0 / empty when the household declares no
        ``transaction_costs`` (the golden household), so the internal shape
        carries no key and round-trips byte-identically (DP#24/DP#32).
    """
    entries = list(doc.get("transaction_costs", []))
    if not entries:
        return 0.0, []
    # Validate every entry first (loud refusals, DP#32) -- a single bad
    # entry refuses the whole block rather than silently dropping it (the
    # exact "parsed, mapped, then never passed" failure this repo exists to
    # prevent). map_transaction_costs is the ONE ingestion boundary for
    # this block; an in-memory config that bypasses the contract path is
    # still held to the same validation by the pure module.
    for entry in entries:
        transaction_costs.validate_transaction_cost(entry, as_of)
    # The FLOORED deployable-seam net (DP#18): a net cost reduces the
    # deployable principal; a net credit is floored to 0.0 here so the
    # deployable principal can never exceed the borrowed lump, and the
    # excess credit is routed as a year-0 savings cash flow instead (a
    # lender credit larger than the fees arrives as savings, not as free
    # investable principal booked against no debt).
    year0_net = transaction_costs.year0_net_refinance_cost(entries, as_of)
    excess_credit = transaction_costs.year0_refinance_excess_credit(
        entries, as_of)
    start_year = int(as_of[:4])
    cash_flows: List[Dict[str, Any]] = []
    if excess_credit > 0.0:
        # Route the excess net refinance credit as a year-0 SAVINGS cash flow
        # (a lender credit larger than the fees). It is a REBATE (a lender
        # credit, not employment income) so it is non-taxable -- the same
        # treatment the mortgage ``cash_back`` uses (#1075). The full borrowed
        # lump stays on the debt side and the deployable principal is unchanged
        # at the seam (floored), so no invested principal appears from nowhere
        # (DP#18 money conservation).
        cash_flows.append({
            "year": start_year,
            "amount": excess_credit,
            "tax_treatment": "non-taxable",
        })
    for entry in entries:
        if "installments" in entry:
            # An installment is NOT a year-0 lump -- it flows as dated cash
            # flows aggregated into calendar years.
            cash_flows.extend(
                transaction_costs.installment_cash_flows(entry, as_of))
            continue
        # A non-installment refinance_origination LUMP at year 0 is the
        # deployable-principal seam (already summed into year0_net, floored);
        # do NOT also emit it as a cash flow (that would double-count it). A
        # non-year-0 refinance_origination lump, and EVERY account_transfer_in
        # lump, flows as a single dated cash flow.
        is_year0_refi_lump = (
            entry["event"] == "refinance_origination"
            and ("date" not in entry or int(entry["date"][:4]) == start_year)
        )
        if is_year0_refi_lump:
            continue
        cash_flows.append(transaction_costs.lump_cash_flow(entry, as_of))
    return year0_net, cash_flows
