"""Transfers and obligations between people, and dated flows in and out.

The blocks here share one shape: a declared entry names PEOPLE, and a name
that resolves to nobody is a typo -- refused loudly -- never a silently
dropped zero (DP#32).

* ``private_loans[]`` (#832) -- a loan FROM AN INDIVIDUAL. The lender is either
  a declared household member or an inline external individual; the borrower is
  always a member. The tax split is applied in the fold, not here, because it
  depends on the lender's age AT each simulation year (DP#1) and on use-gated
  deductibility (DP#25).
* ``gifts[]`` (#841 bite 3) -- a parent->child cash transfer that funds the
  child's OWN registered room. No interest, no repayment, no tax flow.
* ``first_home_purchases[]`` (#704/#931) -- a member becoming a first-time
  buyer, firing their own FHSA qualifying withdrawal and HBP.
* ``installments[]`` (#759) -- fixed-term, zero-interest, NON-compressible
  payment plans.
* ``equity_grants[]`` (#768) -- recorded, valued $0, and said so.
* ``cash_flows[]`` -- dated one-off flows, plus the mortgage's origination
  cash-back as its own first-year entry (#1075).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError


def _map_private_loans(doc: Dict) -> List[Dict[str, Any]]:
    """Issue #832: parse the ``private_loans[]`` block into the internal
    config shape. A private loan is a loan FROM AN INDIVIDUAL -- the lender is
    either a declared household member (a person_id string) or an individual
    OUTSIDE the household (an inline ``{id, relationship?}`` object). The
    borrower is always a declared household member (the household member who
    receives the principal and may deduct the interest).

    Repayment/interest terms default to on_demand/on_demand: a demand loan
    with no interest demanded is modeled as interest-free financing (NO tax
    flow -- no lender income, no borrower deduction). The tax split (lender
    taxable income + borrower s.20(1)(c) deduction when use=investment, with
    s.74.2 minor-lender attribution) applies only when interest IS
    paid/payable this year (interest=paid, or repayment=amortizing which
    implies paid).

    The borrower person_id is validated against the declared ``people[]`` here
    -- a loan naming an undeclared borrower is refused loudly rather than
    silently dropped (DP#32): it is a typo, not a zero loan. A STRING lender
    is likewise validated (it must resolve to a declared person); an OBJECT
    lender is an external individual and is NOT resolved (the engine does not
    tax an external individual). Age-keyed attribution (minor lender ->
    interest attributed to the borrower, ITA s.74.2) is applied in the
    simulation fold, not here, because it depends on the lender's age AT each
    simulation year (DP#1), and use-gated deductibility + interest-payability
    is applied there too (tax decisions, not mapping decisions, DP#25).

    Returns a list of ``{id, lender, lender_is_external, borrower, rate,
    principal, use, repayment, interest}`` dicts (empty when no private loans
    are declared -- the golden household)."""
    people = {p["id"] for p in doc.get("people", [])}
    out: List[Dict[str, Any]] = []
    for loan in doc.get("private_loans", []):
        lender = loan["lender"]
        borrower = loan["borrower"]
        # The borrower is always a declared household member (DP#32: an
        # unresolvable borrower reference is a typo, refused loudly).
        if borrower not in people:
            raise ContractAdaptationError(
                f"private_loan {loan['id']!r} declares borrower={borrower!r}, but no "
                f"person with that id is declared in people[]. The borrower must be a "
                f"household member (issue #832) -- a name that matches nobody is a "
                f"typo, not a zero loan, and is refused rather than silently dropped "
                f"(DP#32). Declared people: {sorted(people)}."
            )
        # The lender is either a declared household member (a person_id string)
        # or an external individual (an inline {id, relationship?} object).
        if isinstance(lender, dict):
            lender_id = lender.get("id")
            lender_is_external = True
            # Carry the external lender's identity; the engine does not tax
            # them (they are not a simulated member), so only the borrower's
            # s.20(1)(c) deduction can apply when interest is paid. The
            # schema's oneOf requires `id` on the inline object, so an
            # inline lender without an id is refused at validate_contract
            # (louder and earlier than a redundant check here).
            lender_internal: Any = {"id": lender_id}
            if lender.get("relationship"):
                lender_internal["relationship"] = lender["relationship"]
        else:
            # A string lender must resolve to a declared person (DP#32).
            if lender not in people:
                raise ContractAdaptationError(
                    f"private_loan {loan['id']!r} declares lender={lender!r}, but no "
                    f"person with that id is declared in people[]. A lender that is a "
                    f"person_id must name an actual household member (issue #832) -- "
                    f"a name that matches nobody is a typo, not a zero loan, and is "
                    f"refused rather than silently dropped (DP#32). To lend from an "
                    f"individual outside the household, declare the lender as an "
                    f"inline {{id, relationship?}} object. Declared people: "
                    f"{sorted(people)}."
                )
            lender_is_external = False
            lender_internal = lender
        out.append({
            "id": loan["id"],
            "lender": lender_internal,
            "lender_is_external": lender_is_external,
            "borrower": borrower,
            "rate": float(loan["rate"]),
            "principal": float(loan["principal"]),
            "use": loan["use"],
            # Defaults: on_demand/on_demand -> interest-free financing (no tax
            # flow). The schema declares these defaults; carrying them here
            # makes the internal shape self-describing (DP#24 round-trip).
            "repayment": loan.get("repayment", "on_demand"),
            "interest": loan.get("interest", "on_demand"),
        })
    return out


def _map_gifts(doc: Dict, member_ids: set, child_ids: set) -> List[Dict[str, Any]]:
    """Epic #841 bite 3: parse the ``gifts[]`` block into the internal config
    shape. A gift is a parent->child cash transfer that FUNDS the child's OWN
    registered contributions (TFSA/FHSA/RRSP) beyond what the child's own small
    income can. It is NOT income to the child and NOT deductible to the donor
    (an inter-vivos cash gift is tax-free to both), so -- unlike a private loan
    -- it carries no interest, no repayment terms, and no tax flow: the fold
    simply REDIRECTS after-tax savings from the household adult base to the
    child's registered room (DP#18: conserved), self-limited by that room.

    Both endpoints are validated here (DP#32: an unresolvable reference is a
    typo, refused loudly, not a silently-dropped zero gift):

    - ``from`` (the DONOR) must be a declared ADULT household member. The
      household models ONE combined adult savings base, so the donor's identity
      is validated but the carve is from that shared base (per-donor base
      tracking is a later bite's asset-location concern).
    - ``to`` (the RECIPIENT) must be a declared CHILD. A gift to an adult or an
      undeclared person cannot fund a child's room and is refused, rather than
      mapped to nobody and silently funding nothing.

    Attribution (ITA s.74.1) is deliberately NOT modeled here and needs no
    handling: a registered-room gift can only reach a member old enough to hold
    TFSA/FHSA room (18+, never a minor), and registered growth is tax-sheltered
    (no income to attribute). s.74.1 would bind only on a future NON-registered
    gift to a minor (bite 3 scope 3, asset location) -- surfaced there, not
    invented here for a case this bite cannot express.

    Returns a list of ``{id, from, to, amount}`` dicts (empty when no gifts are
    declared -- the golden household)."""
    out: List[Dict[str, Any]] = []
    for gift in doc.get("gifts", []):
        donor = gift["from"]
        recipient = gift["to"]
        if donor not in member_ids:
            raise ContractAdaptationError(
                f"gift {gift['id']!r} declares from={donor!r}, but no adult household "
                f"member with that id is declared. The donor of a parent->child gift "
                f"must be a declared adult member (epic #841 bite 3) -- a name that "
                f"matches no member is a typo, not a zero gift, and is refused rather "
                f"than silently dropped (DP#32). Declared members: {sorted(member_ids)}."
            )
        if recipient not in child_ids:
            raise ContractAdaptationError(
                f"gift {gift['id']!r} declares to={recipient!r}, but no child with that "
                f"id is declared. A parent->child gift funds a declared CHILD's own "
                f"registered room (epic #841 bite 3); a gift to an adult or an "
                f"undeclared person funds nobody and is refused rather than mapped to "
                f"nothing (DP#32). Declared children: {sorted(child_ids)}."
            )
        out.append({
            "id": gift["id"],
            "from": donor,
            "to": recipient,
            "amount": float(gift["amount"]),
            # Issue #859 (Part A): a REPAYABLE gift is an intra-family LOAN. It
            # funds the child's room identically (same carve, same room cap,
            # #857), but the principal that lands is a RECEIVABLE the donor
            # keeps (asset) and a LIABILITY the child owes -- booked on the
            # family balance sheet (objective.family_balance_sheet) so a funding
            # loan does NOT reduce the lender's net worth (DP#18), unlike a gift
            # given away. Absent -> False (a plain gift): a modelled default,
            # never a coercion of a supplied value (DP#13/#32).
            "repayable": bool(gift.get("repayable", False)),
        })
    return out


def _map_first_home_purchases(doc: Dict, child_ids: set,
                              adult_ids: set) -> List[Dict[str, Any]]:
    """Issues #704/#931: parse the ``first_home_purchases[]`` block into the
    internal config shape. Each entry declares a member becoming a FIRST-TIME HOME
    BUYER in a given year; the fold fires that member's own FHSA tax-free
    qualifying withdrawal + a non-taxable HBP RRSP withdrawal (15-year repayment)
    toward the down payment -- routed to the CHILD's own accounts
    (``apply_child_first_home_purchases``) or the ADULT's household FHSA / own
    RRSP pots (``apply_adult_first_home_purchases``, issue #931).

    The ``buyer`` must resolve to a declared CHILD or ADULT member (DP#32: an
    unresolvable reference is a typo, refused loudly, not a silently-dropped
    no-op).

    Returns a list of ``{buyer, year}`` dicts (empty when none are declared -- the
    golden household)."""
    valid = child_ids | adult_ids
    out: List[Dict[str, Any]] = []
    for purchase in doc.get("first_home_purchases", []):
        buyer = purchase["buyer"]
        if buyer not in valid:
            raise ContractAdaptationError(
                f"first_home_purchases declares buyer={buyer!r}, but no member with "
                f"that id is declared. A first-time home buyer funds the purchase "
                f"from their OWN FHSA/RRSP and must be a declared child or adult "
                f"(issues #704/#931); a buyer that matches no member is a typo, "
                f"refused rather than silently dropped (DP#32). "
                f"Declared members: {sorted(valid)}."
            )
        out.append({"buyer": buyer, "year": int(purchase["year"])})
    return out


def map_cash_flows(doc: Dict, mortgage: Optional[Dict],
                   start_year: int) -> List[Dict[str, Any]]:
    """``cash_flows[]`` -> the internal dated cash-flow list, plus the
    mortgage's declared origination cash-back as its OWN first-year entry
    (issue #1075)."""
    # Issue #1075 (data-model half): a kind=mortgage tranche's declared
    # lender cash-back (e.g. $1,200 for a $600k+ house tranche) is an
    # UPFRONT cash inflow at origination -- credited as an
    # ``origination_cash_back`` cash-flow in the projection's FIRST year
    # (the mortgage originates at the document's as_of, which is exactly
    # where the fold starts; ``start_year`` is the same calendar-year
    # spelling every declared cash_flow's ``int(date[:4])`` uses, so the
    # engine's ``cf['year'] == sim_year`` test fires it). The inflow is its
    # OWN list entry, independent of any ``cash_flow`` the user declares at
    # start_year: the engine sums cash_flows, so a matching user-declared
    # flow at the same year is credited as its own entry -- never
    # double-credited as the cash-back, and never merged into (or derived
    # from) it. Treated as non-taxable -- a lender rebate, not income --
    # which is the ordinary CRA treatment of a mortgage cash-back
    # incentive. The CONTINGENT
    # clawback (``clawback_rate`` of ``amount`` if the mortgage is fully
    # prepaid before ``term_years``) is NOT priced here: the year-0
    # mortgage amortizes on a fixed schedule the engine cannot early-break,
    # so the event cannot arise; pricing it (the optimizer half of #1075)
    # reads the same ``cash_back`` block back off the contract document.
    legacy_cash_flows = [
        {
            "year": int(cf["date"][:4]),
            "amount": cf["amount"],
            "tax_treatment": "non-taxable" if cf["tax_treatment"] == "tax_free" else "post-tax",
        }
        for cf in doc.get("cash_flows", [])
    ]
    origination_cash_back = mortgage.get("cash_back_total", 0.0) if mortgage else 0.0
    if origination_cash_back > 0:
        legacy_cash_flows.append({
            "year": start_year,
            "amount": origination_cash_back,
            "tax_treatment": "non-taxable",
        })
    return legacy_cash_flows


def map_installments(doc: Dict) -> List[Dict[str, Any]]:
    """``installments[]`` -> the fixed-term, zero-interest payment plans the
    engine services alongside the mortgage and the consumer loans (#759).

    Neither a liability (no drawable facility, no collateral, no prepayment)
    nor a living cost (finite, and NON-compressible under stress). A stated
    rate, a ``non_discretionary: false``, or a start date before the snapshot
    are each refused loudly rather than coerced to the supported value."""
    # ── issue #759: fixed-term, zero-interest installment obligations ──
    # A medical/dental/education payment plan: an up-front lump already paid
    # (not modelled -- it is before the snapshot), then N equal monthly
    # payments and an optional final balloon, at 0% interest, over a FIXED
    # term. This is neither a `liabilities[]` kind (no drawable facility, no
    # collateral, no prepayment/refinance -- it is not a balance-sheet debt)
    # nor `household_budget.annual_living_costs` (which is perpetual AND
    # compressible under stress). Smearing it into annual_living_costs is the
    # current wrong behavior the issue reproduces: a finite, must-pay plan
    # becomes an infinite, discretionary-looking scalar. It is modelled here
    # as a first-class `installments` list the engine services alongside the
    # mortgage + consumer loans (simulation_rules.apply_installments) and
    # folds into the solvency identity's debt-service term + the #758
    # reserve/runway sizing -- the same NON-COMPRESSIBLE channel as the
    # mortgage and consumer-loan payments (contrast #761's discretionary
    # split), so an income-shock year cannot cut it.
    #
    # DP#32 boundary refusals (a partial/unsupported declaration must FAIL
    # LOUDLY, never silently coerce to the supported value):
    #   - `rate` must be 0. A 0%-interest plan is the whole point of this
    #     block; a stated-rate plan's interest pricing/attribution is out of
    #     scope (#759). A non-zero rate is REFUSED, not silently dropped to
    #     0 nor silently honoured.
    #   - `non_discretionary` must be true. An installment plan is a must-pay
    #     contractual outflow; a `false` value (asserting it compresses under
    #     stress) is refused -- the engine has no compressible-installment
    #     path, and silently treating a declared-discretionary plan as rigid
    #     (or vice versa) is exactly the DP#32 two-way trap.
    #   - `start_date` must be >= the document's `as_of`. The up-front lump is
    #     already paid before the snapshot; the simulation models only the
    #     remaining N payments forward from `as_of`. A start_date before
    #     as_of would require re-pricing payments already made -- refusing
    #     loudly is honest, silently dropping the already-paid portion is the
    #     founding defect.
    # No defensive `else: raise` for an unknown kind / missing required field:
    # validate_contract (called at the top of to_internal_config) enforces the
    # schema's required list + additionalProperties:false before this runs, so
    # every plan here is already schema-valid.
    installment_plans: List[Dict[str, Any]] = []
    for plan in doc.get("installments", []):
        rate = plan["rate"]
        if rate != 0:
            raise ContractAdaptationError(
                f"installment plan {plan['id']!r} declares rate={rate}, but "
                f"this block models a 0%-interest plan (issue #759). A stated-"
                f"rate plan's interest pricing/attribution is out of scope; "
                f"silently coercing the rate to 0 OR silently honouring the "
                f"interest would both be the DP#32 defect. Declare rate: 0, "
                f"or track a future stated-rate-installment issue."
            )
        if not plan["non_discretionary"]:
            raise ContractAdaptationError(
                f"installment plan {plan['id']!r} declares "
                f"non_discretionary=false, asserting it compresses under an "
                f"income shock. An installment plan is a must-pay contractual "
                f"outflow that does NOT compress (contrast #761's "
                f"discretionary split); the engine has no compressible-"
                f"installment path. Silently treating a declared-discretionary "
                f"plan as rigid (or a declared-rigid plan as compressible) is "
                f"the DP#32 two-way trap. Declare non_discretionary: true, or "
                f"model the outflow as household_budget.annual_living_costs if "
                f"it is genuinely discretionary."
            )
        if plan["start_date"] < doc["as_of"]:
            raise ContractAdaptationError(
                f"installment plan {plan['id']!r} declares "
                f"start_date={plan['start_date']!r}, before the document's "
                f"as_of={doc['as_of']!r}. The up-front lump is already paid "
                f"before the snapshot; the simulation models only the "
                f"remaining N payments forward from as_of, and re-pricing "
                f"payments already made before the snapshot is not modelled. "
                f"Silently dropping the already-paid portion is the DP#32 "
                f"founding defect. Declare start_date on or after as_of "
                f"(use the date the FIRST remaining monthly payment is due)."
            )
        installment_plans.append({
            "id": plan["id"],
            "owner": plan["owner"],
            "description": plan["description"],
            "start_date": plan["start_date"],
            "monthly_amount": plan["monthly_amount"],
            "number_of_payments": plan["number_of_payments"],
            # final_payment is the one OPTIONAL field: absent = no balloon (a
            # structural absence, not a silent zero -- a $0 balloon is
            # meaningless). Resolved to 0.0 at this contract boundary so the
            # rule reads a uniform key.
            "final_payment": plan.get("final_payment", 0.0),
            "rate": rate,
            "non_discretionary": plan["non_discretionary"],
        })
    return installment_plans


def map_equity_grants(doc: Dict) -> List[Dict[str, Any]]:
    """``equity_grants[]`` -> the private-company grants / stock options.

    A RECORD, not an asset: every grant is valued at $0 for all solvency /
    runway / decumulation metrics, and the output SAYS so, so the household
    knows it was not silently dropped (issue #768, DP#32)."""
    # Issue #768: private-company equity grants / stock options. A RECORD,
    # not an asset: the engine values every declared grant at $0 for all
    # solvency / runway / decumulation metrics (no simulation rule reads
    # this list -- the $0 contribution is by construction), and the output
    # surfaces each grant as 'recorded, valued $0' so the household knows it
    # was not silently dropped (DP#32). Mapped only when declared; an empty
    # list would be a no-op, but emitting it would still pass the internal
    # shape -- kept conditional so the round-trip stays 'absent' for a
    # household with no grants, matching every other optional block (DP#24/
    # DP#32).
    equity_grants: List[Dict[str, Any]] = []
    people_ids = {p["id"] for p in doc.get("people", [])}
    for g in doc.get("equity_grants", []):
        # DP#32: an owner that names no declared person is a typo, not a
        # silently-dropped grant -- refuse loudly.
        if g["owner"] not in people_ids:
            raise ContractAdaptationError(
                f"equity grant {g['id']!r} declares owner={g['owner']!r}, "
                f"but no person with that id is declared. The grant is a "
                f"record on a person -- an owner that names nobody is a typo, "
                f"not a grant with no holder (DP#32). Declared people: "
                f"{sorted(people_ids)}."
            )
        equity_grants.append({
            "id": g["id"],
            "owner": g["owner"],
            "grantor": g["grantor"],
            "grant_date": g["grant_date"],
            "vesting": g["vesting"],
            # strike is nullable (null = TBD); carried through unchanged so
            # the output can say 'strike TBD' rather than invent a value.
            "strike": g.get("strike"),
            "liquidity": g["liquidity"],
            "shares": g.get("shares"),
            "fully_diluted_pct": g.get("fully_diluted_pct"),
            "notes": g.get("notes"),
        })
    return equity_grants
