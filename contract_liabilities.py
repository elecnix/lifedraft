"""The ``liabilities[]`` namespace: every facility the household owes on.

* **The property-secured facilities** -- ``resolve_liability_facilities``
  resolves the ONE mortgage (summing a readvanceable charge's tranches,
  #1075), the ONE HELOC, and the ONE line of credit the downstream engine
  consumes. Anything that cannot be reduced to one facility is refused loudly
  rather than silently keeping the first match (#652/#1075).
* **The closed-end consumer debt** -- ``map_consumer_loans`` maps car /
  student / personal loans onto the list ``simulation_rules
  .apply_consumer_loans`` services. An ``intergenerational_loan``, a
  collateralized consumer loan, and a declared deductible portion are each
  refused loudly, because the engine cannot model them (#763/#703/#656).
* **The signed rate vs. the believed rate** -- ``_reconcile_rate_paths``
  reports every place ``assumptions.rate_paths`` contradicts a SIGNED
  liability rate at year zero. A rate declared on a liability wins at year
  zero, always; the contradiction is surfaced, never silently resolved (#685).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError


def _find_liability(doc: Dict, kind: str, collateral_id: Optional[str] = None) -> Optional[Dict]:
    # issue #652: heloc/line_of_credit are each consumed as a SINGLE facility
    # downstream (one margin_available, one credit_facility_*). kind=mortgage
    # is the ONE exception: issue #1075 lifts the refusal there (multiple
    # tranches of one readvanceable charge are summed by
    # _aggregate_mortgage_facility, below). Returning the FIRST match and
    # dropping the rest is the exact "engine silently substitutes zero"
    # defect class this adapter exists to prevent (DP#32): a document
    # declaring two kind=heloc liabilities would validate, load, and
    # silently drop the second one's limit. So collect every match and
    # REFUSE LOUDLY on >1, the way >1 couple / >1 intergenerational loan
    # already do, rather than silently keeping one. (Faithfully modelling N
    # fixed sub-accounts sharing one charge is a modelling decision -- for
    # the mortgage it is issue #1075; for a revolving facility there is no
    # summed-facility design at all, so refusing beats fabricating.)
    matches = [
        liab for liab in doc.get("liabilities", [])
        if liab["kind"] == kind
        and (collateral_id is None or liab.get("collateral") == collateral_id)
    ]
    if len(matches) > 1:
        raise ContractAdaptationError(
            f"This document declares {len(matches)} kind={kind} liabilities"
            + (f" secured against {collateral_id!r}" if collateral_id is not None else "")
            + f" ({[m['id'] for m in matches]}), but the adapter consumes exactly "
            f"one {kind} facility. Keeping only the first would silently drop the "
            f"rest (understating the household's debt -- the #603/DP#32 'engine "
            f"silently substitutes zero' defect). A readvanceable all-in-one's N "
            f"fixed sub-accounts are summed for kind=mortgage (issue #1075); no "
            f"such summed-facility design exists for a revolving {kind}, so "
            f"declaring more than one is refused. Declare a single {kind}, or "
            f"track #1075."
        )
    return matches[0] if matches else None


def _normalized_mortgage_facility(tranche: Dict) -> Dict:
    """One kind=mortgage liability as the SINGLE downstream facility dict.

    Every kind=mortgage liability -- the sole mortgage of a plain contract
    AND each tranche of a multi-tranche readvanceable charge (issue #1075) --
    is reduced to this one shape before the mapper reads it, so the single-
    and multi-tranche paths share ONE spelling of the downstream facts
    (DP#9) and the new per-tranche flags (``deductible``, ``cash_back``)
    surface identically on both:

      - ``balance.amount`` / ``rate`` / ``amortization``: what the engine
        consumes (mortgage_balance / mortgage_rate / amortization_years).
      - ``deductible_balance`` / ``deductible_interest``: THIS tranche's
        balance, and its EXACT annual interest (balance * its OWN rate),
        when its interest is deductible under ITA s.20(1)(c) (the new
        `deductible` flag), else real 0.0s (DP#32: absence of the flag is
        "not deductible", never a fabricated balance or interest).
      - ``cash_back_total``: this tranche's declared origination cash-back
        amount, else a real 0.0 (no incentive declared, no inflow).

    ``id``/``collateral`` are carried for the rate_paths reconciliation
    (#685) and the charge-limit check (#664) respectively, which name the
    facility the way the raw liability used to.
    """
    balance = tranche["balance"]["amount"]
    cash_back = tranche.get("cash_back")
    return {
        "id": tranche["id"],
        "kind": tranche["kind"],
        "balance": {"amount": balance, "as_of": tranche["balance"]["as_of"]},
        "rate": tranche["rate"],
        "rate_type": tranche["rate_type"],
        "amortization": tranche["amortization"],
        "collateral": tranche.get("collateral"),
        "deductible_balance": balance if tranche.get("deductible") else 0.0,
        "deductible_interest": balance * tranche["rate"] if tranche.get("deductible") else 0.0,
        "cash_back_total": cash_back["amount"] if cash_back else 0.0,
    }


def _aggregate_mortgage_facility(
    doc: Dict, collateral_id: Optional[str], *, aggregate_tranches: bool
) -> Optional[Dict]:
    """The document's kind=mortgage facilities as ONE downstream facility.

    Issue #1075 (data-model half): a readvanceable all-in-one mortgage is a
    SINGLE registered charge split into N fixed sub-accounts (a house
    tranche, a deductible investment tranche, ...) -- the faithful encoding
    is N ``kind=mortgage`` liabilities sharing one ``collateral``. The
    downstream engine consumes ONE amortizing mortgage (one
    ``mortgage_balance`` / ``mortgage_rate`` / ``amortization_years``), so
    the adapter sums the tranches:

      - balances sum into the single ``mortgage_balance``;
      - the rate is the balance-weighted average (the summed facility's
        total interest is then EXACTLY what the tranches charge separately:
        rate is linear in balance, so no interest is invented or lost);
      - ``deductible_balance`` accumulates each tranche's balance whose new
        ``deductible`` flag is set, and ``deductible_interest`` accumulates
        those tranches' EXACT annual interest (each balance * its OWN rate --
        the weighted-average rate times the summed balance only coincides
        with that sum when every tranche shares one rate). Both surface on
        the config as ``property.deductible_mortgage_balance`` /
        ``property.deductible_mortgage_interest`` for the s.20(1)(c)
        interest pricing (issue #850) to consume;
      - ``cash_back_total`` accumulates each tranche's declared cash-back,
        surfaced as an ``origination_cash_back`` cash-flow in the first
        projection year.

    DP#32 loud refusals (never a silent substitute):

      - tranches secured against DIFFERENT collaterals are two charges, not
        two tranches of one -- summing them would fabricate a single
        mortgage out of unrelated debts, so it is refused loudly;
      - the multi-tranche lift is PRINCIPAL-ONLY (``aggregate_tranches``):
        the summed facility lands in the PRINCIPAL's ``mortgage_balance``,
        so more than one mortgage the principal does NOT secure is refused
        loudly rather than folded into the family home's debt -- a
        rental/cottage charge is not a tranche of the principal's
        readvanceable, and folding it into the principal's balance would
        misattribute the household's debt (DP#32). The unfiltered fallback
        lookup (``aggregate_tranches=False``) still returns a SINGLE legacy
        mortgage that declares no collateral, but keeps the #652 loud >1
        refusal for anything beyond that;
      - tranches with DIFFERENT amortization terms cannot share the one
        downstream schedule (unlike the rate, a term is NOT linear in
        balance, so no single years figure preserves every tranche's
        payment -- that is a genuine modelling decision, and refusing is
        the honest answer until a decision exists);
      - a zero TOTAL balance makes the weighted-average rate undefined
        (0/0) -- refused loudly rather than guessed.

    ``collateral_id`` follows ``_find_liability``'s convention: None means
    "no filter" (every kind=mortgage liability in the document), a value
    restricts to tranches registered against that property. The caller
    tries the principal's id first (``aggregate_tranches=True`` -- the only
    lookup that may sum >1) and falls back to the unfiltered lookup
    (``aggregate_tranches=False`` -- >1 is refused), exactly as the
    pre-#1075 single-facility lookup did.
    """
    matches = [
        liab for liab in doc.get("liabilities", [])
        if liab["kind"] == "mortgage"
        and (collateral_id is None or liab.get("collateral") == collateral_id)
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return _normalized_mortgage_facility(matches[0])
    collaterals = {m.get("collateral") for m in matches}
    if len(collaterals) > 1:
        raise ContractAdaptationError(
            f"This document declares {len(matches)} kind=mortgage liabilities "
            f"({[m['id'] for m in matches]}) against DIFFERENT collaterals "
            f"({sorted(str(c) for c in collaterals)}). The adapter models ONE "
            f"amortizing mortgage facility (mortgage_balance/mortgage_rate); "
            f"summing mortgages that secure DIFFERENT charges would fabricate a "
            f"single debt out of unrelated ones -- the #603/DP#32 'engine "
            f"silently substitutes zero' defect in reverse. Only tranches of "
            f"ONE readvanceable charge (a shared `collateral`) can be summed "
            f"(issue #1075). Declare the tranches against the same property, "
            f"or model the other mortgage as a separate charge the engine can "
            f"represent (e.g. a property's `financing`)."
        )
    if not aggregate_tranches:
        raise ContractAdaptationError(
            f"This document declares {len(matches)} kind=mortgage liabilities "
            f"({[m['id'] for m in matches]}) sharing one collateral "
            f"({sorted(str(c) for c in collaterals)}) that is NOT the "
            f"principal's charge. The adapter models ONE amortizing mortgage "
            f"facility on the principal's charge (mortgage_balance/"
            f"mortgage_rate), and the multi-tranche lift of issue #1075 -- "
            f"summing the fixed sub-accounts of one readvanceable charge -- "
            f"exists ONLY for the principal's own charge: folding another "
            f"property's debt into the family home's mortgage_balance would "
            f"misattribute the household's debt (DP#32). The #652 loud "
            f"refusal therefore stands for every multi-mortgage document the "
            f"principal does not secure. Declare a single mortgage, or track "
            f"#1075."
        )
    terms = {m["amortization"]["years"] for m in matches}
    if len(terms) > 1:
        raise ContractAdaptationError(
            f"This document declares {len(matches)} kind=mortgage tranches sharing "
            f"one charge ({[m['id'] for m in matches]}) with DIFFERENT amortization "
            f"terms ({sorted(terms)} years). The engine amortizes the summed "
            f"mortgage_balance on ONE schedule; unlike the rate (linear in balance, "
            f"so the balance-weighted average preserves every tranche's interest "
            f"exactly), no single years figure preserves every tranche's payment, "
            f"so picking one -- or averaging -- would silently reprice N signed "
            f"contracts (DP#32). Align the tranches' amortization_years, or track "
            f"#1075 for a multi-schedule engine."
        )
    total = sum(m["balance"]["amount"] for m in matches)
    if total <= 0:
        raise ContractAdaptationError(
            f"This document declares {len(matches)} kind=mortgage tranches "
            f"({[m['id'] for m in matches]}) whose balances sum to ${total:,.0f}. "
            f"The balance-weighted average rate is undefined for a zero total "
            f"(0/0) -- refused loudly rather than guessed (DP#32). A paid-off "
            f"mortgage declares a single zero-balance liability, not N of them."
        )
    weighted_rate = sum(m["balance"]["amount"] * m["rate"] for m in matches) / total
    first = matches[0]
    facility = _normalized_mortgage_facility(first)
    facility["balance"] = {"amount": total, "as_of": first["balance"]["as_of"]}
    facility["rate"] = weighted_rate
    facility["deductible_balance"] = sum(
        m["balance"]["amount"] for m in matches if m.get("deductible"))
    facility["deductible_interest"] = sum(
        m["balance"]["amount"] * m["rate"] for m in matches if m.get("deductible"))
    facility["cash_back_total"] = sum(
        m["cash_back"]["amount"] for m in matches if m.get("cash_back"))
    return facility


# ── Signed rate (fact) vs. rate_paths (belief) — issue #685 ───────────────
#
# `liabilities[].rate` is a FACT: it is what this household pays today, off a
# signed contract, and a statement settles it. `assumptions.rate_paths.*` is a
# BELIEF: what the borrowing is expected to cost over the horizon, which no
# document fixes decades out (the schema's own `$defs/rate_path` x-uncertainty
# says exactly this: "only your CURRENT signed rate is a fact (see
# liability.rate)").
#
# Before #685 the belief SILENTLY WON at year zero: `to_internal_config` mapped
# `rate_paths.heloc.rate` onto `assumptions.heloc_rate`, which is
# `simulation_config.resolve_heloc_rate`'s FIRST tier — so every optimizer,
# ranking and reporting consumer priced the household's Smith-Manoeuvre spread
# off the belief, while `simulation.py`'s engine charged the declared
# `property.heloc_rate` (#654). One run, two different HELOC rates, no warning.
# A stale `rate_paths` block — in the case that surfaced this, the household's
# PREVIOUS lender's rate — projected 44 years at a cost of debt that
# contradicted the mortgage the engine had loaded one function earlier.
#
# The precedence is now explicit, and it is not negotiable: **a rate declared on
# a liability wins at year zero, always.** A `rate_paths` entry describes what
# happens AFTER the current term — it never repricess a rate the contract has
# already pinned. And when the two disagree about year zero, that is a
# contradiction in the user's own input, so it is surfaced (a `logger.warning`
# here, plus a `model_fidelity` Approximation naming the liability, both rates
# and the winner, on every output surface) rather than silently resolved.

def _rate_path_year0(path: Dict) -> Optional[float]:
    """The rate a `$defs/rate_path` belief asserts for the CURRENT year.

    `fixed` asserts one rate for every year, so year zero is `rate`.
    `variable`/`forecast` assert a year-indexed `path`, so year zero is
    `path[0]`. An empty `path` asserts nothing about year zero — that is
    absence, and it returns None (DP#32: absence is not zero).
    """
    if path["type"] == "fixed":
        return path["rate"]
    series = path["path"]  # schema-required for variable/forecast
    return series[0] if series else None


def _reconcile_rate_paths(rate_paths: Dict,
                          liabilities_by_kind: Dict[str, Optional[Dict]]) -> List[Dict]:
    """Every place a `rate_paths` BELIEF contradicts a SIGNED rate at year zero.

    Returns one record per contradiction: the liability whose declared rate the
    belief disagrees with, both rates, and which one the run uses (always the
    declared one). An empty list means the document is self-consistent — the
    belief either agrees with the contract at year zero or the contract does not
    pin that rate at all (no such liability declared), which is the only reading
    under which a `rate_paths` entry is free to supply year zero itself.

    Pure (DP#3): no logging, no mutation. The caller decides how loudly to say
    it. `model_fidelity.rate_path_conflicts()` reads the records back off the
    internal config so every report surface can name the same figures.
    """
    conflicts: List[Dict] = []
    for kind, liab in liabilities_by_kind.items():
        path = rate_paths.get(kind)
        if liab is None or path is None:
            continue
        believed = _rate_path_year0(path)
        declared = liab["rate"]  # schema-required on mortgage/heloc
        if believed is None or believed == declared:
            continue
        conflicts.append({
            "liability_id": liab["id"],
            "liability_kind": kind,
            "declared_rate": declared,
            "believed_rate": believed,
            "winner": "declared",
        })
    return conflicts


def resolve_liability_facilities(doc: Dict, principal: Optional[Dict]) -> tuple:
    """The household's three borrowing facilities, as
    ``(mortgage, heloc, credit_facility)``.

    The mortgage is looked up against the principal's charge FIRST (the only
    lookup that may SUM a readvanceable all-in-one's tranches, #1075) and
    falls back to the unfiltered lookup for a legacy document whose mortgage
    declares no collateral. The HELOC follows the same order. A
    ``line_of_credit`` is NOT looked up by collateral -- it need not be tied to
    the principal at all, which is the whole point of the unsecured case
    (#689)."""
    # issue #1075 (data-model half): the mortgage is now aggregated -- N
    # kind=mortgage tranches sharing one collateral (a readvanceable
    # all-in-one's fixed sub-accounts) are summed into the single downstream
    # facility (balance-weighted rate, summed deductible balance/interest,
    # summed cash-back). The multi-tranche lift is PRINCIPAL-ONLY: the
    # summed facility lands in the principal's mortgage_balance, so only
    # the principal-collateral lookup may sum >1 (aggregate_tranches=True).
    # Lookup ORDER is unchanged from pre-#1075: try the principal's
    # collateral first, fall back to the unfiltered lookup (legacy documents
    # whose mortgage declares no collateral). The fallback
    # (aggregate_tranches=False) keeps the #652 loud >1 refusal -- a
    # document with NO principal-secured mortgage and several others must
    # refuse loudly even when they share one collateral, never silently fold
    # a rental/cottage debt into the family home's balance (DP#32; see
    # _aggregate_mortgage_facility).
    mortgage = None
    if principal is not None:
        mortgage = _aggregate_mortgage_facility(
            doc, principal["id"], aggregate_tranches=True)
    if mortgage is None:
        mortgage = _aggregate_mortgage_facility(
            doc, None, aggregate_tranches=False)
    heloc = _find_liability(doc, "heloc", principal["id"] if principal else None) \
        or _find_liability(doc, "heloc")
    # issue #689: a revolving line of credit, secured or not. NOT looked up
    # by collateral_id the way mortgage/heloc are above -- a line_of_credit
    # is not necessarily tied to `principal` at all (that is the whole point
    # of the unsecured case a real household's signed facility needed to
    # represent). The first declared line_of_credit liability is this
    # household's facility; only one is supported today, matching the
    # single-facility pattern mortgage/heloc already have.
    credit_facility = _find_liability(doc, "line_of_credit")
    return mortgage, heloc, credit_facility


def map_consumer_loans(doc: Dict) -> List[Dict[str, Any]]:
    """The closed-end consumer liabilities (car_loan / student_loan /
    personal_loan) as the first-class ``consumer_loans`` list
    ``simulation_rules.apply_consumer_loans`` amortizes year by year."""
    # ── issue #763: closed-end consumer liabilities (car_loan, student_loan,
    # personal_loan) -- amortizing consumer debt with a balance, a rate, a
    # fixed monthly payment and a payoff date. Before this block they were
    # schema-valid, parsed, schema-validated, accepted -- and then SILENTLY
    # DROPPED before the engine saw them: their balance, rate and
    # payment_monthly never reached debt service, the solvency identity
    # (#679) or the runway metric (#758). That is the exact "engine silently
    # substitutes zero" defect class this repo exists to kill (DP#32), and
    # the contract-reachability detector measured every one of their leaves
    # as DROPPED at the adapter.
    #
    # They are NOT aliased onto the mortgage (DP#7: model the mechanism, not
    # the branded product): a consumer loan is a separate, unsecured,
    # closed-end amortizing liability the household services alongside the
    # mortgage -- distinct from the revolving HELOC/line_of_credit and from
    # the property-secured mortgage. Mapped into a first-class
    # `consumer_loans` list the engine amortizes year by year
    # (simulation_rules.apply_consumer_loans) and folds into the solvency
    # identity's debt-service term and the reserve/runway sizing (#758).
    #
    # interest is NOT deductible (a car/student/personal loan finances
    # consumption, not income-earning property): the #656 default-to-
    # deductible defect is guarded here -- an absent `deductibility` defaults
    # to non-deductible (investment_portion=0), and a declared investment_portion
    # > 0 is REFUSED LOUDLY rather than silently coerced either way. Deductible
    # consumer-loan interest tracing (ITA s.20(1)(c) on a borrowed-to-invest
    # personal loan) is the #703 lender-identity sub-problem, not this issue.
    consumer_loans: List[Dict[str, Any]] = []
    for liab in doc.get("liabilities", []):
        kind = liab["kind"]
        if kind in ("mortgage", "heloc", "line_of_credit"):
            continue  # handled above -- property-secured / revolving facilities
        if kind == "intergenerational_loan":
            # #703: the lender-identity field this kind needs (who the loan is
            # owed to -- a family member, with its own estate/attribution
            # semantics) is not yet modelled. Accept-and-ignore is not an
            # acceptable state (DP#32), so the contract is refused LOUDLY,
            # naming the kind and the blocking issue -- never silently dropped.
            raise ContractAdaptationError(
                f"liability {liab['id']!r} is kind=intergenerational_loan, "
                f"which the engine cannot yet consume: its lender-identity "
                f"field (#703) is not modelled, so loading it would silently "
                f"drop a real debt the household declared. Refusing rather "
                f"than substituting zero (DP#32). Track #703 for this kind; "
                f"until then declare it as a personal_loan if its semantics "
                f"fit, or omit it."
            )
        # No defensive `else: raise` for an unknown kind here: to_internal_config
        # calls validate_contract(doc) at the top, which rejects any kind not in
        # the schema's liability_kind enum before this loop runs -- so by here
        # `kind` is guaranteed to be one of the enum values, all of which are
        # handled above (mortgage/heloc/line_of_credit skipped, intergenerational
        # refused, car/student/personal mapped). The "every enum kind is
        # consumed-or-refused" invariant is enforced by tests/architecture/
        # test_contract_reachability.py's GATE 4 against the live schema enum,
        # not by an unreachable branch here (DP#9: no dead code).
        # A consumer loan secured against real estate is a different product
        # (it would count against the property's registered charge, #664/#689)
        # -- the engine models consumer loans as UNSECURED. A non-null
        # collateral is refused loudly rather than silently ignored (DP#32):
        # the household stated a fact the engine would otherwise mis-model.
        if liab.get("collateral") is not None:
            raise ContractAdaptationError(
                f"liability {liab['id']!r} (kind={kind}) declares "
                f"collateral={liab['collateral']!r}: a consumer loan secured "
                f"against real estate is a different product from the unsecured "
                f"amortizing consumer debt this engine models (#763). Its "
                f"balance would belong in the property's registered charge "
                f"(#664/#689), not in consumer_loans. Declare collateral=null, "
                f"or model the facility as a line_of_credit/heloc."
            )
        # #656/#703 guard: deductibility is honored by refusing the case the
        # engine cannot model. An absent deductibility defaults to
        # non-deductible (the safe direction -- consumption debt is not
        # deductible); a declared investment_portion > 0 is refused loudly
        # rather than silently dropped or silently made deductible.
        deductibility = liab.get("deductibility")
        if deductibility is not None and deductibility["investment_portion"] > 0:
            raise ContractAdaptationError(
                f"liability {liab['id']!r} (kind={kind}) declares "
                f"deductibility.investment_portion={deductibility['investment_portion']:.4f} "
                f"> 0, asserting some of its interest is deductible (ITA "
                f"s.20(1)(c)). This engine does not model deductible interest "
                f"on a closed-end consumer loan -- the s.20(1)(c) tracing path "
                f"lives on the readvanceable HELOC (#656/#703), and silently "
                f"honouring OR silently ignoring this declaration would both be "
                f"the DP#32 default-to-deductible / drop-the-declaration defect. "
                f"Refusing loudly. Declare investment_portion=0 (consumption "
                f"debt, not deductible) or track #703."
            )
        amort = liab["amortization"]  # schema-required on closed-end kinds
        consumer_loans.append({
            # `id`/`kind` travel with the loan for reporting/traceability; the
            # engine's amortization reads only the four numeric facts below.
            "id": liab["id"],
            "kind": kind,
            "balance": liab["balance"]["amount"],
            "rate": liab["rate"],
            "payment_monthly": amort["payment_monthly"],
            "amortization_years": amort["years"],
        })
    return consumer_loans
