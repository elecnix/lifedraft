"""One-time transaction costs and credits attached to a financial event.

The engine prices recurring flows to the dollar but had NO concept of a
one-time transaction cost or credit attached to an event -- refinance
origination (discharge/quittance fees, notary/legal, title insurance,
appraisal) was undeclarable and the cash-out flows in gross; account
transfers had no fee-out / reimbursement / match-bonus modeling. The only
existing members of this class were a property sale's ``selling_costs`` and
a mortgage's ``cash_back`` origination credit (#1075), each ad-hoc.

This module owns the GENERAL mechanism's pure arithmetic (DP#3: same inputs
-> same output, no hidden state, no globals):

* :func:`validate_transaction_cost` -- the loud-validation contract (DP#32).
  A negative amount, an unknown direction, a date before the document's
  ``as_of``, and a missing date/anchor are each refused rather than producing
  a plausible wrong number. The schema's enum/minimum/oneOf refuse the same
  shapes at validation; this re-checks them at the adapter boundary so a
  config assembled in code cannot reach the engine with a bad shape.
* :func:`year0_net_refinance_cost` -- the signed NET year-0 lump cost of a
  refinance origination (costs minus credits, lump entries only -- an
  installment is NOT a year-0 lump). Positive = a net cost that REDUCES the
  refinance cash-out's deployable principal (so NET proceeds are what
  deploys); negative = a net credit that INCREASES it (a lender credit that
  adds free money to the advance). The engine applies this as a year-0-
  equivalent reduction of the deployable principal -- the SAME seam #137's
  deployment-lag carry uses (see ``FamilySimulation``'s year-0 ``fill_room``
  call), reused rather than reinvented.
* :func:`installment_cash_flows` -- for an entry with an installment
  schedule, the N equal monthly payments aggregated into calendar years
  from the entry's dated point. An installment is NOT a year-0 lump: the
  payments flow as dated cash flows (the existing cash_flows mechanism), so
  a match bonus paid over 12 months projects to a DIFFERENT trajectory than
  the same bonus paid as a year-0 lump (less year-0 compounding).

Absence of any ``transaction_costs`` declaration is byte-identical (DP#32):
the golden household declares none, ``year0_net_refinance_cost`` returns
``0.0`` and no installment cash flows are produced, so the deployable
principal and the cash_flows list are untouched.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, List

# The closed set of events a one-time cost/credit may attach to. A new event
# is a real modelling decision (where the cost/credit deploys), not a free
# string (DP#32). Kept in sync with the schema's
# ``transaction_cost_event`` enum (schema/defs/transaction_costs.json).
TRANSACTION_COST_EVENTS = frozenset({"refinance_origination", "account_transfer_in"})

# The closed set of directions. A cost REDUCES net proceeds; a credit
# INCREASES it. An unknown value is refused loudly (DP#32 two-way: never
# silently coerce an unknown direction to the favourable value). Kept in
# sync with the schema's ``direction`` enum.
TRANSACTION_COST_DIRECTIONS = frozenset({"cost", "credit"})

# The closed set of event-relative anchors. 'year_0' = the document's as_of
# (the start of the projection). Kept in sync with the schema's ``anchor``
# enum.
TRANSACTION_COST_ANCHORS = frozenset({"year_0"})

# The closed set of tax treatments a transaction cost/credit may carry.
# ``non_taxable`` = a rebate/reimbursement (a lender credit, a transfer-fee
# reimbursement) -- NOT income, flows as a non-taxable savings cash flow (the
# same treatment the mortgage ``cash_back`` uses, #1075). ``post_tax`` = a
# TAXABLE bonus (a match bonus on an account transfer-in) -- ordinary employment
# income, marked so the engine's cash-flow mechanism can tax it differently
# from a rebate (the rebate-vs-bonus distinction). Kept in sync with the
# schema's ``tax_treatment`` enum. The default (absent key) is ``non_taxable``:
# a cost/credit is a rebate unless declared otherwise (DP#13: a default is a
# fallback for absent input, never an opinion that coerces a supplied value).
TRANSACTION_COST_TAX_TREATMENTS = frozenset({"non_taxable", "post_tax"})

# The default tax treatment when a transaction_costs[] entry omits the field.
# A cost/credit is a non-taxable rebate unless the household declares it a
# taxable bonus (DP#13/DP#32: the default is a fallback for absent input, and
# a declared 0-equivalent -- non_taxable -- round-trips byte-identical).
DEFAULT_TAX_TREATMENT = "non_taxable"


def validate_transaction_cost(entry: Dict, as_of: str) -> None:
    """The loud-validation contract for one ``transaction_costs[]`` entry
    (DP#32: absence must fail loudly, never produce a plausible wrong
    number).

    Refuses, naming the bad value in every case:

    * ``amount < 0`` -- a negative magnitude is a bad input, not a sign
      (the ``direction`` field carries the sign). The schema's ``money``
      ``minimum: 0`` refuses this too; re-checked here so an in-memory
      config cannot reach the engine with a negative amount.
    * ``direction`` not in ``{cost, credit}`` -- an unknown direction is
      refused rather than silently coerced to the favourable value (the
      schema's enum refuses it too; re-checked here).
    * ``event`` not in ``TRANSACTION_COST_EVENTS`` -- a new event is a real
      modelling decision, not a free string.
    * a ``date`` before ``as_of`` -- a cost/credit paid before the snapshot
      cannot be priced forward (the up-front lump is already paid); refused
      rather than silently dropped (the same DP#32 founding defect
      ``installments`` refuses).
    * neither ``date`` nor ``anchor`` present, or both present -- the
      schema's ``oneOf`` enforces exactly one; re-checked here.
    * ``tax_treatment`` present but not in ``{non_taxable, post_tax}`` -- an
      unknown tax treatment is refused rather than silently coerced to the
      default (the schema's enum refuses it too; re-checked here). A declared
      ``non_taxable`` is the default spelled explicitly (byte-identical); a
      declared ``post_tax`` marks the entry as a TAXABLE bonus (the
      rebate-vs-bonus distinction).

    A declared ``amount: 0`` is a REAL value (a $0 cost/credit) and is
    honoured explicitly -- it carries no effect and round-trips byte-
    identically (DP#32: 0 is a value, not a fallback).

    Args:
        entry: one ``transaction_costs[]`` dict (already schema-validated
            when loaded through the contract path; re-validated here so an
            in-memory config is held to the same contract).
        as_of: the document's ``as_of`` date (ISO ``YYYY-MM-DD``); a
            ``date`` before this is refused.
    """
    amount = entry["amount"]
    if amount < 0:
        raise ValueError(
            f"transaction_costs[id={entry.get('id')!r}] declares amount={amount}, "
            f"which is negative. The amount is a NON-NEGATIVE magnitude and the "
            f"sign is carried by `direction` (cost | credit); a negative amount "
            f"is a bad input, not a sign-flipped cost, and is refused rather "
            f"than producing a plausible wrong number (DP#32)."
        )
    direction = entry["direction"]
    if direction not in TRANSACTION_COST_DIRECTIONS:
        raise ValueError(
            f"transaction_costs[id={entry.get('id')!r}] declares "
            f"direction={direction!r}, which is not a known direction. The "
            f"direction carries the sign of the entry (cost = a reduction, "
            f"credit = an addition); an unknown value is refused rather than "
            f"silently coerced to the favourable value (DP#32). Valid "
            f"directions: {sorted(TRANSACTION_COST_DIRECTIONS)}."
        )
    event = entry["event"]
    if event not in TRANSACTION_COST_EVENTS:
        raise ValueError(
            f"transaction_costs[id={entry.get('id')!r}] declares event={event!r}, "
            f"which is not a known event. The event selects where the cost/credit "
            f"deploys; a new event is a real modelling decision, not a free "
            f"string (DP#32). Valid events: {sorted(TRANSACTION_COST_EVENTS)}."
        )
    has_date = "date" in entry
    has_anchor = "anchor" in entry
    if has_date and has_anchor:
        raise ValueError(
            f"transaction_costs[id={entry.get('id')!r}] declares BOTH `date` "
            f"({entry['date']!r}) and `anchor` ({entry['anchor']!r}). Exactly "
            f"one is allowed: `date` for an actual calendar date, `anchor` for "
            f"an event-relative anchor (DP#32: one spelling of the fact)."
        )
    if not has_date and not has_anchor:
        raise ValueError(
            f"transaction_costs[id={entry.get('id')!r}] declares NEITHER `date` "
            f"nor `anchor`. Exactly one is required: the dated point the "
            f"cost/credit is paid (an actual `date`, or an `anchor` for a "
            f"year-0 event whose exact date the household has not stated)."
        )
    if has_date:
        if entry["date"] < as_of:
            raise ValueError(
                f"transaction_costs[id={entry.get('id')!r}] declares "
                f"date={entry['date']!r}, before the document's as_of={as_of!r}. "
                f"A cost/credit paid before the snapshot cannot be priced "
                f"forward (the up-front lump is already paid); refusing rather "
                f"than silently dropping the already-paid portion is the DP#32 "
                f"founding defect. Declare date on or after as_of."
            )
    else:
        anchor = entry["anchor"]
        if anchor not in TRANSACTION_COST_ANCHORS:
            raise ValueError(
                f"transaction_costs[id={entry.get('id')!r}] declares "
                f"anchor={anchor!r}, which is not a known anchor. Valid "
                f"anchors: {sorted(TRANSACTION_COST_ANCHORS)}."
            )
    # tax_treatment is OPTIONAL: absent = non_taxable (a rebate, the default).
    # When present, it must be a known value -- an unknown treatment is
    # refused rather than silently coerced to the favourable (non-taxable)
    # value (DP#32 two-way). A declared non_taxable is the default spelled
    # explicitly (byte-identical); a declared post_tax marks a TAXABLE bonus.
    if "tax_treatment" in entry:
        tt = entry["tax_treatment"]
        if tt not in TRANSACTION_COST_TAX_TREATMENTS:
            raise ValueError(
                f"transaction_costs[id={entry.get('id')!r}] declares "
                f"tax_treatment={tt!r}, which is not a known tax treatment. "
                f"A cost/credit is a non-taxable rebate (non_taxable, the "
                f"default) or a taxable bonus (post_tax); an unknown value is "
                f"refused rather than silently coerced to the favourable "
                f"value (DP#32). Valid treatments: "
                f"{sorted(TRANSACTION_COST_TAX_TREATMENTS)}."
            )


def _entry_start_date(entry: Dict, as_of: str) -> _date:
    """The calendar date the entry's installment schedule (or lump) starts.

    A ``date`` entry starts on its declared date; a ``year_0`` anchor starts
    on the document's ``as_of`` (the start of the projection). Pure (DP#3).
    """
    if "date" in entry:
        return _date.fromisoformat(entry["date"])
    return _date.fromisoformat(as_of)


def _signed_amount(entry: Dict) -> float:
    """The entry's amount with the direction's sign applied: a cost is
    negative (a reduction), a credit is positive (an addition). Pure (DP#3)."""
    amount = float(entry["amount"])
    return -amount if entry["direction"] == "cost" else amount


def _cash_treatment_of(entry: Dict) -> str:
    """The INTERNAL cash-flow ``tax_treatment`` value for an entry, mapped
    from the schema's underscore spelling to the hyphenated spelling the
    engine's cash-flow mechanism reads (``cf.get('tax_treatment', 'post-
    tax')``).

    ``non_taxable`` (the default, also when the key is absent) -> ``non-
    taxable``: a rebate/reimbursement, NOT income -- flows as a non-taxable
    savings cash flow (the same treatment the mortgage ``cash_back`` uses,
    #1075). ``post_tax`` -> ``post-tax``: a TAXABLE bonus (a match bonus on
    an account transfer-in) -- ordinary employment income, marked so the
    engine's cash-flow mechanism can distinguish it from a rebate (the
    rebate-vs-bonus distinction, DP#27). Pure (DP#3)."""
    tt = entry.get("tax_treatment", DEFAULT_TAX_TREATMENT)
    return "post-tax" if tt == "post_tax" else "non-taxable"


def _raw_year0_refinance_net(entries: List[Dict], as_of: str) -> float:
    """The SIGNED net year-0 refinance-origination LUMP cost BEFORE the
    deployable-seam floor (DP#18). Positive = net cost, negative = net
    credit, 0.0 = none. Internal helper shared by :func:`year0_net_refinance_
    cost` (the floored deployable-seam value) and :func:`year0_refinance_
    excess_credit` (the excess credit routed as savings). Pure (DP#3)."""
    start_year = int(as_of[:4])
    net = 0.0
    for entry in entries:
        # Each entry is validated by the caller (map_transaction_costs) before
        # this helper runs; year0_net_refinance_cost re-validates too. Do not
        # re-validate here -- the two public entry points own the loud refusals.
        if entry["event"] != "refinance_origination":
            continue
        if "installments" in entry:
            # An installment is NOT a year-0 lump -- it flows as dated cash
            # flows (see installment_cash_flows). Excluded from the year-0
            # deployable-principal reduction.
            continue
        # Only a YEAR-0 lump reduces the year-0 deployable principal. A
        # refinance_origination dated to a later year is a later event (the
        # engine wires only the year-0 attachment point in this layer); it
        # flows as a dated cash flow via lump_cash_flow, not the seam.
        if "date" in entry and int(entry["date"][:4]) != start_year:
            continue
        # Sign convention for the deployable-principal reduction: a
        # COST contributes +amount (reduces net proceeds -> the engine
        # subtracts this from the lump, REDUCING the deployable principal);
        # a CREDIT contributes -amount (increases net proceeds ->
        # subtracting a negative ADDS to the deployable principal). This is
        # the OPPOSITE of the cash-flow sign convention (_signed_amount,
        # where a cost is a negative outflow): the deployable-principal
        # reduction is a reduction-of-principal convention, so the sign is
        # flipped deliberately here.
        amt = float(entry["amount"])
        net += -amt if entry["direction"] == "credit" else amt
    return net


def year0_net_refinance_cost(entries: List[Dict], as_of: str) -> float:
    """The floored NET year-0 LUMP cost of a refinance origination, as the
    DEPLOYABLE-SEAM value (DP#18 money conservation).

    Sums every ``event=refinance_origination`` entry that has NO installment
    schedule (a LUMP, paid at the year-0 anchor or a year-0 date) with the
    deployable-principal sign convention: a cost contributes ``+amount`` (it
    reduces net proceeds), a credit contributes ``-amount`` (it increases net
    proceeds). The RAW signed net is then FLOORED at zero:

    * ``max(net, 0.0)`` -- a net COST that REDUCES the deployable principal so
      NET proceeds are what deploys (the gross cash-out advance less the
      origination costs). This is the ONLY value ever applied at the
      deployable seam (``deployable_lump = lump_sum - ... - transaction_cost_
      year0``).
    * a net CREDIT (raw net < 0) is NOT applied here -- it is floored to 0.0
      so the deployable principal can NEVER exceed the borrowed lump while the
      debt side stays at the full lump (DP#18: $X of invested principal must
      not appear from nowhere). The excess credit is routed instead as a
      DATED SAVINGS cash flow via :func:`year0_refinance_excess_credit` (a
      lender credit larger than the fees arrives as savings, not as free
      investable principal booked against no debt).
    * ``0.0`` = no declared refinance origination lumps, costs exactly
      offset credits, OR a net credit (all floored) -- byte-identical to the
      pre-feature path at the seam (DP#32).

    Installment refinance_origination entries are EXCLUDED here -- an
    installment is NOT a year-0 lump; it flows as dated cash flows (see
    :func:`installment_cash_flows`). A ``refinance_origination`` entry whose
    ``date`` is NOT in the projection's first calendar year is also excluded
    from the year-0 lump (it is not a year-0 event); such an entry flows as a
    dated cash flow via :func:`lump_cash_flow`.

    Pure function (DP#3): no fold state, no globals. Each entry is validated
    first (loud refusals, DP#32).

    Args:
        entries: the ``transaction_costs[]`` list (already schema-validated
            on the contract path; each entry is re-validated here).
        as_of: the document's ``as_of`` date (ISO ``YYYY-MM-DD``); the year-0
            calendar year is ``int(as_of[:4])``.

    Returns:
        The floored net year-0 lump cost (``max(raw_net, 0.0)``): a non-
        negative net cost that reduces the deployable principal, or 0.0 when
        none declared / costs offset credits / a net credit (floored). The
        excess credit (raw net < 0) is available from
        :func:`year0_refinance_excess_credit`.
    """
    for entry in entries:
        validate_transaction_cost(entry, as_of)
    return max(_raw_year0_refinance_net(entries, as_of), 0.0)


def year0_refinance_excess_credit(entries: List[Dict], as_of: str) -> float:
    """The EXCESS net year-0 refinance-origination CREDIT, routed as a dated
    SAVINGS cash flow instead of inflating the deployable principal (DP#18).

    When credits exceed costs among the year-0 refinance-origination lumps,
    the raw net is negative (a net credit). :func:`year0_net_refinance_cost`
    floors that at 0.0 at the deployable seam so the deployable principal
    never exceeds the borrowed lump; this function returns the magnitude of
    the floored-away credit (``max(-raw_net, 0.0)``) so the adapter can route
    it as a year-0 SAVINGS cash flow -- a lender credit larger than the fees
    arrives as savings the household can deploy, NOT as free investable
    principal booked against no debt. 0.0 when there is no net credit (costs
    exceed or offset credits, or none declared).

    Pure function (DP#3). Each entry is validated first (loud refusals, DP#32).

    Args:
        entries: the ``transaction_costs[]`` list.
        as_of: the document's ``as_of`` date (ISO ``YYYY-MM-DD``).

    Returns:
        The excess net credit (``max(-raw_net, 0.0)``) to route as a year-0
        savings cash flow. 0.0 when costs exceed or offset credits.
    """
    for entry in entries:
        validate_transaction_cost(entry, as_of)
    return max(-_raw_year0_refinance_net(entries, as_of), 0.0)


def lump_cash_flow(entry: Dict, as_of: str) -> Dict[str, Any]:
    """A non-installment entry that is NOT a year-0 refinance_origination
    lump, as a single dated cash flow (the existing cash_flows mechanism).

    A non-installment ``account_transfer_in`` entry (fee-out cost,
    reimbursement credit) flows as a YEAR-0 (or dated) cash flow -- a cost
    reduces the year's savings, a credit adds to it. A non-year-0
    ``refinance_origination`` lump flows as a dated cash flow too (the engine
    wires only the year-0 refinance attachment point in this layer; a later
    refinance origination is a later cash flow, not the deployable seam).

    The cash flow's ``tax_treatment`` is driven by the entry's declarable
    ``tax_treatment`` field (``non_taxable`` = a rebate/reimbursement,
    ``post_tax`` = a taxable bonus) instead of a hardcoded value, so a match
    bonus (post_tax) is MARKED taxable -- distinct from a non-taxable rebate
    -- and the engine's cash-flow mechanism can apply the rebate-vs-bonus
    distinction (DP#27). The default (absent key) is ``non_taxable`` (a
    cost/credit is a rebate unless declared a bonus, DP#13).

    Pure (DP#3). The entry is assumed already validated.
    """
    start = _entry_start_date(entry, as_of)
    return {
        "year": start.year,
        "amount": _signed_amount(entry),
        "tax_treatment": _cash_treatment_of(entry),
    }


def installment_cash_flows(entry: Dict, as_of: str) -> List[Dict[str, Any]]:
    """An installment entry's N equal monthly payments aggregated into
    calendar years, as dated cash flows (the existing cash_flows mechanism).

    The amount is split into ``installments.months`` EQUAL monthly payments
    starting at the entry's dated point (its ``date``, or the document's
    ``as_of`` when ``anchor`` is ``year_0``). Each monthly payment is added
    to the calendar year its month falls in, so a 12-month schedule starting
    mid-year-0 spans TWO calendar years and an 18-month schedule spans three
    -- producing a genuinely DIFFERENT trajectory from the same amount paid
    as a year-0 lump (which would all land in year 0). The sign is the
    direction's (cost = negative, credit = positive).

    The monthly amount is ``amount / months`` (exact division); the calendar-
    year aggregation sums the monthly amounts whose month falls in that year.
    A schedule that ends after the projection horizon contributes only the
    months that fall within it -- the cash_flows mechanism drops years past
    the horizon naturally (a ``cf['year'] == sim_year`` test that never
    matches). The cash flow's ``tax_treatment`` is driven by the entry's
    declarable ``tax_treatment`` field (``non_taxable`` rebate vs ``post_tax``
    bonus) instead of a hardcoded value (DP#27).

    Pure function (DP#3): same inputs -> same output, no fold state. The
    entry is assumed already validated (the caller runs
    :func:`validate_transaction_cost` first).

    Args:
        entry: a ``transaction_costs[]`` dict carrying an ``installments``
            block.
        as_of: the document's ``as_of`` date (the anchor start when the
            entry uses ``anchor: year_0``).

    Returns:
        A list of ``{year, amount, tax_treatment}`` cash-flow dicts, one per
        calendar year the schedule touches, in ascending year order. Empty
        when ``months == 0`` (the schema forbids this, but the function is
        defensive: a 0-month schedule produces no payments, never a
        divide-by-zero).
    """
    months = entry["installments"]["months"]
    if months <= 0:
        return []
    monthly = float(entry["amount"]) / months
    signed_monthly = -monthly if entry["direction"] == "cost" else monthly
    start = _entry_start_date(entry, as_of)
    by_year: Dict[int, float] = {}
    for i in range(months):
        # Month i is `start` shifted forward by i whole months. Same
        # year-rollover helper contract_decisions._add_months uses (no
        # dateutil dependency).
        m = (start.month - 1 + i) % 12 + 1
        y = start.year + (start.month - 1 + i) // 12
        by_year[y] = by_year.get(y, 0.0) + signed_monthly
    return [
        {"year": y, "amount": by_year[y], "tax_treatment": _cash_treatment_of(entry)}
        for y in sorted(by_year)
    ]
