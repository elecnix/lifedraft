"""The ``people[]`` namespace: who the household is, and what each person
becomes in the internal config.

Three jobs, in the order ``to_internal_config`` needs them:

1. **Selection** -- ``_find_primary_and_spouse`` picks the ONE couple the
   engine's two-slot compute drives, and ``admit_people`` partitions everybody
   else into dependent children, additional accumulating adults, and a loud
   refusal for anyone the engine could only hold by dropping a fact about them
   (issues #698/#899).
2. **Dated income** -- an employment income that has not started at ``as_of``
   pays nothing today but IS reached by the projection, so it becomes a dated
   ``income_segments`` entry rather than a flattened $0 (issue #653).
3. **Mapping** -- ``_map_member`` / ``_map_child`` turn one person into the
   member dict ``SimState.initial`` seeds, and ``_map_registered_balances``
   attributes every rrsp/tfsa/spousal_rrsp/fhsa opening balance to its owner
   or refuses loudly (issue #647: money conservation across the boundary).

``_owner_shares`` lives here too: an owner_ref is a reference to PEOPLE (a
bare person id, or a ``{'joint': [...]}`` percentage split -- #601), and
accounts, properties and liabilities all resolve one.
"""
from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError, ContractValidationError


def _people_by_id(doc: Dict) -> Dict[str, Dict]:
    return {p["id"]: p for p in doc["people"]}


def _find_primary_and_spouse(doc: Dict) -> (str, Optional[str]):
    """Pick ONE couple to drive the legacy engine (documented Phase 1
    limitation -- see module docstring). Preference order: the person named
    by ``decisions.horizon.person`` (and their spouse, if any); else the
    first ``spouse_of`` pair found; else the first person alone."""
    people = _people_by_id(doc)
    # The horizon person is the documented first preference. It is
    # schema-required on a validated contract, but this helper is also called
    # directly on synthetic partial docs (e.g. issue #823's contract-mapping
    # tests) that carry no ``decisions`` block at all. Fall back to the
    # documented remaining preference order (first ``spouse_of`` pair, else
    # the first person alone) so an absent horizon degrades gracefully instead
    # of raising a KeyError that masks the loud failure the caller is about to
    # raise (DP#32: a missing preference must not crash the wrong layer).
    decisions = doc.get("decisions")
    horizon = decisions.get("horizon") if decisions is not None else None
    horizon_person = horizon.get("person") if horizon is not None else None
    if horizon_person and horizon_person in people:
        primary_id = horizon_person
    else:
        primary_id = None
        for pid, p in people.items():
            if any(r["type"] == "spouse_of" for r in p.get("relationships", [])):
                primary_id = pid
                break
        if primary_id is None:
            primary_id = next(iter(people), None)

    spouse_id = None
    if primary_id is not None:
        for r in people[primary_id].get("relationships", []):
            if r["type"] == "spouse_of":
                spouse_id = r["person"]
                break
        if spouse_id is None:
            for pid, p in people.items():
                for r in p.get("relationships", []):
                    if r["type"] == "spouse_of" and r["person"] == primary_id:
                        spouse_id = pid
                        break
    return primary_id, spouse_id


def _parent_of_targets(doc: Dict) -> set:
    """Every person named as the target of ANY ``parent_of`` relationship in the
    document -- i.e. every dependent descendant, of any generation, not only the
    selected couple's direct children (issue #698). A grandchild (the child of a
    child) is in this set exactly like a direct child, which is what lets the
    N-child machinery (epic #841 + Step 6 of #643) hold an extra generation of
    dependents once the couple-only boundary is relaxed."""
    targets = set()
    for p in doc["people"]:
        for r in p.get("relationships", []):
            if r["type"] == "parent_of":
                targets.add(r["person"])
    return targets


def _needs_adult_compute(doc: Dict, person_id: str, person: Dict) -> bool:
    """True when a person carries a fact only the ADULT compute path can hold, so
    representing them through the dependent-CHILD seam (``_map_child``) would
    silently drop it (issue #698 / #643 Step 8).

    After Steps 2-7 the account stores (#700), the per-adult tax loop (#701) and
    the N-return estate (#705) all iterate members, and the N-child machinery
    (epic #841 + Step 6) gives every dependent child its own seeded/grown
    accounts and an individually-computed return. What is STILL two-slot is the
    ADULT compute itself: ``SimState.initial`` seeds exactly primary+spouse, the
    ``YearWorkingState`` carries two RRSP/TFSA scalars, and
    ``simulate_year_pure``'s signature is primary/spouse marginal rate -- the
    N-slot rule interface Step 5 explicitly deferred. ``config.adults()``
    therefore resolves exactly the selected couple, so a THIRD adult would be
    frozen (no growth/contribution) and untaxed -- the silent-drop DP#32 defect
    the boundary guard exists to prevent.

    The facts below are exactly the ``_map_member`` fields ``_map_child`` does
    NOT carry: a spouse pairing (=> a second couple, spousal RRSP / income
    splitting / survivor estate), decumulated retirement benefits
    (cpp/oas/employer_pension), a future-dated employment segment (the child seam
    keeps only today's scalar), and an independent retirement-age candidacy.
    Any one of them means the person needs the not-yet-built N-adult compute
    (#706 / Step 9), so the boundary refuses them LOUDLY rather than admit-and-
    drop."""
    if any(r["type"] == "spouse_of" for r in person.get("relationships", [])):
        return True
    benefits = person.get("benefits", {})
    if any(benefits.get(k) for k in ("cpp", "oas", "employer_pension")):
        return True
    # Issue #650: a pre-claim CPP/OAS entitlement is a decumulated retirement
    # benefit-to-be -- the same adult-only fact _map_child would silently drop,
    # so it must trip the adult-compute boundary exactly as an in-pay benefit.
    entitlements = person.get("entitlements", {})
    if any(entitlements.get(k) for k in ("cpp", "oas")):
        return True
    if _future_employment_segments(person, doc["as_of"]):
        return True
    for cand in doc["decisions"]["retirement_age"]:  # both keys schema-required
        if cand["person"] == person_id and cand["candidate_ages"]:
            return True
    return False


#: Issue #899 (part a): the retirement age assumed for an extra adult who
#: declares no retirement_age candidacy. Mirrors the engine's own default
#: (countries.canada.retirement_transition.DEFAULT_RETIREMENT_AGE); duplicated
#: here as a plain constant so the contract boundary needs no engine import
#: (DP#25). An extra adult with no candidacy therefore reaches this age -- and
#: is refused unless it lands beyond the horizon (the pure-accumulator gate).
_DEFAULT_RETIREMENT_AGE = 65


def _is_pure_accumulator(doc: Dict, person_id: str, person: Dict,
                         horizon_end_year: int) -> bool:
    """Issue #899 (part a): True when an extra ADULT (a person carrying an
    adult-only fact, ``_needs_adult_compute``) is a pure ACCUMULATOR across the
    ENTIRE horizon, so the newly-uncapped accumulation-only compute can hold
    them without inventing any decumulation model.

    This is the safety boundary of #899's split. The mechanical uncap of the
    two-slot compute to N adults is defined ONLY for adults who never
    decumulate; a retired / benefit-drawing / soon-to-retire non-primary adult
    needs a spending-target + mortality model that is deliberately OUT OF SCOPE
    here and tracked in **#901**. So an extra adult is admitted only when ALL of:

    - they draw NO CPP / OAS / employer pension (any declared benefit means they
      are decumulating, or about to -- refuse to #901);
    - they own NO decumulation account (rrif / lif / lira / dcpp / dbpp), which
      the accumulation-only store has no slot for;
    - they do NOT reach retirement age within the horizon -- their retirement
      age (the min declared candidate age, else ``_DEFAULT_RETIREMENT_AGE``)
      lands in a calendar year AFTER the last simulated year, so they are still
      contributing on the final year (never crossing into decumulation).

    A person whose birth year is unknown cannot have this proven, so they are
    refused rather than assumed (DP#32: absence fails loudly, it is not coerced
    into a convenient default)."""
    benefits = person.get("benefits", {})
    if any(benefits.get(k) for k in ("cpp", "oas", "employer_pension")):
        return False
    # Issue #650: a pre-claim entitlement means this adult WILL decumulate
    # (draw CPP/OAS) once they claim -- out of scope for the accumulation-only
    # compute (#901), same as an in-pay benefit above.
    entitlements = person.get("entitlements", {})
    if any(entitlements.get(k) for k in ("cpp", "oas")):
        return False
    for acc in doc.get("accounts", []):
        if acc.get("owner") == person_id and acc["kind"] in (
                "rrif", "lif", "lira", "dcpp", "dbpp"):
            return False
    if not person.get("birth_date"):
        return False
    birth_year = int(person["birth_date"][:4])
    retirement_age = _DEFAULT_RETIREMENT_AGE
    for cand in doc["decisions"]["retirement_age"]:  # both schema-required
        if cand["person"] == person_id and cand["candidate_ages"]:
            retirement_age = min(cand["candidate_ages"])
    # Reaches retirement in the year birth_year + retirement_age; admitted only
    # when that is strictly after the last simulated (horizon-end) year.
    return birth_year + retirement_age > horizon_end_year


def _horizon_end_year(doc: Dict, primary_id: str) -> Optional[int]:
    """Issue #899 (part a): the last simulated calendar year -- the year the
    primary reaches ``decisions.horizon.until_age``. Returns None when the
    horizon does not date against the primary (no birth year, or the horizon is
    declared against a different person), in which case an extra adult cannot be
    proven a pure accumulator and is refused."""
    horizon = doc["decisions"]["horizon"]  # schema-required
    if horizon["person"] != primary_id:
        return None
    primary = _people_by_id(doc).get(primary_id, {})
    if not primary.get("birth_date"):
        return None
    return int(primary["birth_date"][:4]) + horizon["until_age"]


def _age_at(birth_date: Optional[str], as_of: str) -> Optional[int]:
    if not birth_date:
        return None
    b = _date.fromisoformat(birth_date)
    a = _date.fromisoformat(as_of)
    return a.year - b.year - ((a.month, a.day) < (b.month, b.day))


def _active_employment_income(person: Dict, as_of: str) -> float:
    total = 0.0
    for inc in person.get("incomes", []):
        if inc["kind"] != "employment":
            continue
        if inc["from"] and inc["from"] > as_of:
            continue
        if inc["to"] and inc["to"] < as_of:
            continue
        total += inc["amount"]
    return total


def _future_employment_segments(person: Dict, as_of: str) -> List[Dict]:
    """Issue #653: the employment incomes whose ``from`` starts strictly AFTER
    ``as_of`` -- returned as dated ``income_segments`` for the engine.

    ``_active_employment_income`` (rightly) skips these: a job that has not
    started pays nothing on the snapshot date, so it contributes nothing to the
    flat base scalar. But the projection begins at ``as_of`` and reaches that
    start date within its first year(s); flattening the schedule to one scalar
    read the earner's salary as $0 for the WHOLE horizon (top marginal rate
    halved, RRSP deductions worthless, the ranked strategy silently flipped).

    Modelling each as an ``income_segments`` entry -- the same mechanism #674
    added for job-loss shocks -- lets ``simulation.py``'s
    ``_income_components_for_year`` turn the income ON in the calendar year it
    actually begins (DP#1: derive the year's income from the stored ``[from,
    to)`` window every time, never a flat whole-horizon number). An income
    active at ``as_of`` stays in the base scalar and produces NO segment, so
    this is a strict no-op unless a future start date is declared.
    """
    segments = []
    for inc in person.get("incomes", []):
        if inc["kind"] != "employment":
            continue
        if not (inc["from"] and inc["from"] > as_of):
            continue
        segments.append({
            "kind": inc["kind"],
            "amount": inc["amount"],
            "from": inc["from"],
            "to": inc["to"],
        })
    return segments


def _owner_shares(owner) -> Dict[str, float]:
    """{person_id: fraction} for an account/property/liability owner_ref --
    a bare person id, or a {'joint': [{person, pct}, ...]} split (#601)."""
    if isinstance(owner, dict):
        return {j["person"]: j["pct"] for j in owner["joint"]}
    return {owner: 1.0}


# ── Account-kind coverage (issue #647): every one of the schema's twelve
# ``account_kind`` values must either reach the engine or refuse loudly --
# no silent drop may remain expressible (DP#32).
#
# Three buckets:
#  - _HOUSEHOLD_AGGREGATED_KINDS: kinds with their own dedicated,
#    multi-account-aware aggregation in contract_accounts.map_account_pots
#    (non_reg/resp/lira/lif/lsif) -- skipped here, not double-mapped.
#  - _ENGINE_UNREPRESENTABLE_KINDS: kinds the engine (SimState/
#    simulate_year_pure) has NO field for at all -- not a per-owner limit,
#    a genuine structural gap (#643). Always refused.
#  - rrsp/tfsa/spousal_rrsp/fhsa: mapped per-owner below, onto the
#    primary/spouse "member" dicts SimState.initial() already reads.
_HOUSEHOLD_AGGREGATED_KINDS = frozenset({"non_reg", "resp", "lira", "lif", "lsif"})
_ENGINE_UNREPRESENTABLE_KINDS = frozenset({"rrif", "dcpp", "dbpp"})


def _map_registered_balances(doc: Dict, primary_id: str, spouse_id: Optional[str],
                             child_ids: List[str],
                             extra_adult_ids: List[str] = ()) -> Dict[str, Dict[str, float]]:
    """The per-owner opening balance for every ``rrsp``/``tfsa``/``fhsa``/
    ``spousal_rrsp`` account in the document (issue #647): ``{person_id:
    {internal_field: amount}}``.

    Money conservation across the ingestion boundary (#574's invariant,
    reintroduced by #647 through this very mapper): every dollar declared
    under one of these four kinds either lands in the returned dict, or this
    function raises ``ContractAdaptationError``. There is no third outcome
    -- the previous version of this loop silently fell through for
    ``spousal_rrsp``/``fhsa`` (and every kind beyond rrsp/tfsa), which is
    exactly how a shipped example lost $72,000 with no error.

    Household-pooled kinds (non_reg/resp/lira/lif/lsif) are handled by their
    own dedicated aggregation in ``contract_accounts.map_account_pots``, not
    silently re-mapped by a second path here.
    """
    valid_owners = {primary_id, spouse_id, *child_ids, *extra_adult_ids} - {None}
    balances: Dict[str, Dict[str, float]] = {pid: {} for pid in valid_owners}

    for acc in doc.get("accounts", []):
        kind = acc["kind"]
        if kind in _HOUSEHOLD_AGGREGATED_KINDS:
            continue
        amount = acc["balance"]["amount"]
        owner = acc.get("owner")

        if kind in _ENGINE_UNREPRESENTABLE_KINDS:
            raise ContractAdaptationError(
                f"Account {acc['id']!r} has kind={kind!r}. The engine "
                f"(SimState / simulate_year_pure) has no field to represent "
                f"a {kind!r} account at all -- a structural gap (#643), not "
                f"a per-owner limit. Mapping it anywhere would silently "
                f"drop ${amount:,.2f}. Remove the account from the "
                f"document, or wait for #643's engine rewrite to land."
            )

        elif kind in ("rrsp", "tfsa"):
            # Issue #841 bite 1: a child is now a first-class savings subject.
            # A child-owned rrsp/tfsa opening balance lands on that child's
            # own per-member dict (via _map_child, which already carried the
            # `balances.get(person_id)` wiring for this day) exactly as an
            # adult's does -- no longer refused. An owner that resolves to NO
            # declared person is still refused loudly below (DP#32): the
            # child-saver promotion is not a licence to silently absorb a
            # balance owned by nobody the document declares.
            if (owner not in (primary_id, spouse_id) and owner not in child_ids
                    and owner not in extra_adult_ids):
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={kind}) is owned by "
                    f"{owner!r}, which is not a declared person in this "
                    f"document (not the primary, the spouse, any child, or an "
                    f"admitted additional accumulating adult). "
                    f"Mapping it anywhere would silently drop ${amount:,.2f} "
                    f"(DP#32)."
                )
            field = f"{kind}_balance"
            balances[owner][field] = balances[owner].get(field, 0) + amount

        elif kind == "spousal_rrsp":
            if spouse_id is None or owner != spouse_id:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind=spousal_rrsp) is owned by "
                    f"{owner!r}. The engine models exactly one spousal-RRSP "
                    f"pot, structurally tied to the SPOUSE's RRIF minimum "
                    f"(simulation_rules.py) -- a spousal RRSP owned by "
                    f"anyone else (the primary, a child) has no pot to map "
                    f"into (#643). Mapping it anyway would silently drop "
                    f"${amount:,.2f}."
                )
            balances[spouse_id]["spousal_rrsp_balance"] = (
                balances[spouse_id].get("spousal_rrsp_balance", 0) + amount)

        elif kind == "fhsa":
            if owner in child_ids:
                # Issue #841 bite 1: a child-owned FHSA is that child's OWN
                # first-home savings goal. It is routed to the child's own
                # per-member dict -- a distinct savings subject from the
                # single ADULT household FHSA pot below -- so the one-adult-
                # pot constraint (which exists to stop two ADULT owners being
                # merged) does not, and must not, apply across generations.
                balances[owner]["fhsa_balance"] = balances[owner].get("fhsa_balance", 0) + amount
            elif owner in (primary_id, spouse_id):
                # Issue #700/#643/#704 (Step 4): the engine now holds a per-adult
                # FHSA store (simulation_state.adult_fhsa), so a second adult's
                # FHSA is representable -- each adult's balance is attributed to
                # their OWN member dict and seeded into its own slot, which then
                # compounds independently. (Per-adult FHSA CONTRIBUTION/room
                # allocation and HBP-per-owner are the tracked Step 4 follow-up;
                # opening balances and growth are handled.)
                balances[owner]["fhsa_balance"] = balances[owner].get("fhsa_balance", 0) + amount
            else:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind=fhsa) is owned by "
                    f"{owner!r}, which is not a declared person in this "
                    f"document (not the primary, the spouse, or any child). "
                    f"Mapping it anywhere would silently drop ${amount:,.2f} "
                    f"(DP#32)."
                )

        else:
            # Defensive, not reachable while the schema's account_kind enum
            # stays at exactly the twelve values every branch above (plus
            # _HOUSEHOLD_AGGREGATED_KINDS) accounts for -- see
            # tests/test_input_contract.py::test_overlay_tightens_account_
            # kind_to_a_closed_enum. If the enum ever grows, the new kind
            # must refuse here rather than silently fall through (DP#32).
            raise ContractAdaptationError(
                f"Account {acc['id']!r} has kind={kind!r}, which this "
                f"mapper does not yet classify as mapped, household-"
                f"aggregated, or engine-unrepresentable. Update "
                f"_map_registered_balances rather than let it fall through."
            )

    return balances


def _tuition_by_year(doc: Dict, p: Dict, role: str, person_id: str) -> Dict[int, float]:
    """Issue #764: expand each study_period's `tuition` into a per-calendar-year
    map of eligible tuition paid, for the SIMPLE case (a student claiming
    their OWN federal credit). Returns {} when no tuition is declared.

    `tuition` is the eligible tuition paid PER calendar year the study period
    is in session. A study period with `end_date` pays that tuition in every
    year in [start_year, end_year]; a period with `end_date: null` (ongoing/
    unknown end) pays it in the start_year only -- annualising it to infinity
    would be a fabrication, so a null end is treated as a single known year.

    DP#32 (the load-time loud-warning half of #764): the SIMPLE case models
    only a taxed member (primary/spouse) claiming their OWN federal credit.
    A thing a declared tuition can mean that this PR does NOT yet model is
    warned about loudly here, never silently dropped:
      - a CHILD declaring tuition: children are not taxed as individuals
        (#701), so a child's own credit has no tax to reduce -- the transfer
        to a supporting spouse/parent (federal $5,000 limit) is the real
        mechanism and is NOT yet modelled (follow-up filed). The child's
        tuition is recorded but NOT applied.
    The QUEBEC provincial tuition credit (TP-1 Schedule T, 8%) is NOW modelled
    (#783) -- `tuition_tax_credit` adds it for a Quebec resident, sourced
    from the year-versioned `qc_tuition_credit_rate` -- so the pre-#783 QC
    warning is removed. Carry-forward of unused amounts is not modelled
    (follow-up).
    """
    sp = p.get("study_periods")
    if not sp:
        return {}
    out: Dict[int, float] = {}
    declared_any = False
    child_tuition = role == "child"
    for s in sp:
        tuition = s.get("tuition")
        if tuition is None:
            continue
        declared_any = True
        start_year = int(s["start_date"][:4])
        end = s.get("end_date")
        # null end = single known year (see docstring); do not annualise to infinity.
        years = range(start_year, (int(end[:4]) + 1) if end else (start_year + 1))
        for yr in years:
            out[yr] = out.get(yr, 0.0) + float(tuition)
    # Issue #785: the child-tuition warning is REMOVED -- transfer to a
    # supporting spouse/parent (federal $5,000 limit) is now modelled. A
    # child's tuition is no longer just recorded; it is TRANSFERRED to the
    # designated supporter (reducing their tax), and the excess carries
    # forward (#784). Issue #783: the Quebec PROVINCIAL tuition credit (TP-1
    # Schedule T, 8%) is also now modelled.
    return out


def _map_member(doc: Dict, person_id: str, role: str,
                registered_balances: Dict[str, Dict[str, float]]) -> Dict:
    people = _people_by_id(doc)
    p = people[person_id]
    as_of = doc["as_of"]
    member: Dict[str, Any] = {
        "role": role,
        # Issue #785: the person_id, so the prologue can resolve a child's
        # `transfer_to` (a person_id) to the right member.
        "id": person_id,
        "gross_income": _active_employment_income(p, as_of),
    }
    # Issue #653: an employment income that starts AFTER as_of is not in the
    # base scalar above (it pays $0 on the snapshot date), but the projection
    # reaches its start date -- carry it as a dated income_segment so the
    # engine turns it on in the right calendar year instead of reading the
    # salary as $0 forever. Absent (no future income) => key omitted => no-op.
    future_segments = _future_employment_segments(p, as_of)
    if future_segments:
        member["income_segments"] = future_segments
    if p.get("birth_date"):
        member["birth_year"] = int(p["birth_date"][:4])
    # Note (issue #100): a person admitted as a SIMULATED ADULT member must have
    # a dateable birth date, or the retirement gate silently maps them as
    # 'never retires' (zero CPP/OAS/pension). That loud refusal lives at the
    # CONTRACT BOUNDARY in ``map_members`` below (where a person is actually
    # admitted as an engine member), NOT here -- _map_member is a pure mapper
    # also exercised directly by unit tests that legitimately omit birth_date
    # (e.g. a tuition-by-year mapping test). The schema sanctions birth_date:
    # null only for a deceased ancestor, who is never admitted as a member.

    room = p["room"]  # schema-required on every person -- never absent post-validation
    for kind in ("rrsp", "tfsa", "fhsa"):
        grant = room.get(kind)
        if grant is not None:
            member[f"{kind}_room_accumulated"] = grant["contribution_room"]

    # #647: every rrsp/tfsa/spousal_rrsp/fhsa account this person owns --
    # computed once, for everyone, by _map_registered_balances (which
    # refuses loudly for anything it cannot represent rather than silently
    # dropping it).
    member.update(registered_balances.get(person_id, {}))

    # 'benefits' itself is genuinely OPTIONAL on a person (schema: not in
    # person's required list) -- a person who hasn't started drawing any
    # benefit omits the whole object. .get(key, default) is the correct
    # DP#32 idiom here (default for an ABSENT key), not `or` (which would
    # also fire on a present-but-empty {} -- here that's the same thing, but
    # written the honest way on principle).
    benefits = p.get("benefits", {})
    cpp = benefits.get("cpp")
    if cpp:
        member["cpp_start_age"] = _age_at(p.get("birth_date"), cpp["start_date"])
        member["cpp_monthly_estimated"] = cpp["monthly_amount"]
    oas = benefits.get("oas")
    if oas:
        member["oas_start_age"] = _age_at(p.get("birth_date"), oas["start_date"])
        member["oas_defer_months"] = oas["defer_months"]
    pension = benefits.get("employer_pension")
    if pension:
        member["pension_income_annual"] = pension["annual_amount"]

    # Issue #650: a FUTURE, pre-claim CPP/OAS entitlement -- the number off a
    # Service Canada statement for a person who has NOT started drawing yet.
    # Distinct from the in-pay `benefits` block above (a committed start_date):
    # an entitlement carries the estimate + a MODELED claim age, and lands on
    # exactly the same read keys (cpp_monthly_estimated / cpp_start_age /
    # oas_start_age / oas_defer_months) member_retirement_income already
    # consumes, so the engine begins the benefit at the modeled claim age.
    # `entitlements` is optional; an absent object sets no keys => pure no-op.
    entitlements = p.get("entitlements", {})
    ent_cpp = entitlements.get("cpp")
    if ent_cpp:
        if cpp:
            raise ContractValidationError(
                f"person {person_id!r} declares BOTH an in-pay benefits.cpp "
                f"(already claimed) AND a pre-claim entitlements.cpp (not yet "
                f"claimed) -- a person is one or the other, never both. Keep "
                f"the in-pay benefit if CPP is flowing, else the entitlement."
            )
        member["cpp_monthly_estimated"] = ent_cpp["estimated_monthly_at_65"]
        member["cpp_start_age"] = ent_cpp["claim_age"]
    ent_oas = entitlements.get("oas")
    if ent_oas:
        if oas:
            raise ContractValidationError(
                f"person {person_id!r} declares BOTH an in-pay benefits.oas "
                f"(already claimed) AND a pre-claim entitlements.oas (not yet "
                f"claimed) -- a person is one or the other, never both. Keep "
                f"the in-pay benefit if OAS is flowing, else the entitlement."
            )
        claim_age = ent_oas["claim_age"]
        member["oas_start_age"] = claim_age
        # DP#32: state the claim age ONCE. The deferral bonus is DERIVED from
        # it (65 = no deferral), never carried as a second, driftable field.
        member["oas_defer_months"] = (claim_age - 65) * 12

    for cand in doc["decisions"]["retirement_age"]:  # both schema-required
        if cand["person"] == person_id and cand["candidate_ages"]:
            member["retirement_age"] = cand["candidate_ages"][0]

    # Issue #764 (simple case): carry this taxed member's eligible tuition
    # per year so simulation can apply their OWN federal tuition credit.
    # See _tuition_by_year for the DP#32 load-time warnings on what is NOT yet
    # modelled (QC provincial, carry-forward, transfer). Children are handled
    # in _map_child (recorded + warned, not applied -- they have no own tax).
    tuition_by_year = _tuition_by_year(doc, p, role, person_id)
    if tuition_by_year:
        member["tuition_by_year"] = tuition_by_year
        # Issue #785: extract the declared transfer target (the supporting
        # spouse/parent the student transfers unused credit to, ITA s.118.8
        # $5,000 limit). A taxed member transfers only AFTER applying to own
        # tax; a child transfers the full credit (no own tax, #701).
        transfer_to = _tuition_transfer_to(p)
        member.update({"tuition_transfer_to": transfer_to} if transfer_to else {})

    # Issue #141: this person's DECLARED superficial-loss dispositions ride
    # the member record wholesale (family.members round-trips verbatim,
    # DP#24 -- config_serde untouched). Shape/affiliation validation lives
    # in ``map_members``, which knows the full admitted-member set.
    declared = p.get("superficial_losses")
    if declared:
        member["superficial_losses"] = [dict(e) for e in declared]

    return member


def _tuition_transfer_to(p: Dict) -> Optional[str]:
    """Issue #785: extract the ``transfer_to`` person_id from the first
    study_period that declares tuition (the student transfers unused credit
    to this person, ITA s.118.8, $5,000 federal limit). None when no transfer
    is declared."""
    return next(
        (s["transfer_to"] for s in p.get("study_periods", [])
         if s.get("tuition") is not None and s.get("transfer_to")),
        None,
    )


def _map_child(doc: Dict, person_id: str,
              registered_balances: Dict[str, Dict[str, float]]) -> Dict:
    p = _people_by_id(doc)[person_id]
    child: Dict[str, Any] = {
        "role": "child",
        "name": p.get("label", person_id),
        # Issue #813: the person_id, so the simulation prologue can resolve a
        # private loan's lender/borrower (person_ids) to the right child.
        "id": person_id,
        "gross_income": _active_employment_income(p, doc["as_of"]),
    }
    if p.get("birth_date"):
        child["birth_year"] = int(p["birth_date"][:4])
    room = p["room"]
    for kind in ("rrsp", "tfsa", "fhsa"):
        grant = room.get(kind)
        if grant is not None:
            child[f"{kind}_room_accumulated"] = grant["contribution_room"]
    # #647 / #841 bite 1: the child's own rrsp/tfsa/fhsa opening balances.
    # _map_registered_balances now attributes a child-owned rrsp/tfsa/fhsa
    # account to that child here (children are first-class savings subjects,
    # #841) -- {} only when the child owns no such account (the golden
    # household). A spousal_rrsp owned by a child is still refused loudly
    # there, and an account owned by no declared person is refused too (DP#32).
    child.update(registered_balances.get(person_id, {}))
    sp = p.get("study_periods")
    if sp:
        child["study_periods"] = [
            {"institution": s["institution"], "program": s["program"],
             "start_year": int(s["start_date"][:4]),
             "end_year": int(s["end_date"][:4]) if s["end_date"] else None}
            for s in sp
        ]
    # Issue #785: a child declaring tuition is now TRANSFERRED to a
    # supporting parent/spouse (up to the federal $5,000 limit), not just
    # recorded-and-warned. The child's own credit is computed and transferred
    # in the simulation prologue; the excess carries forward (#784).
    child_tuition = _tuition_by_year(doc, p, "child", person_id)
    if child_tuition:
        child["tuition_by_year"] = child_tuition
        transfer_to = _tuition_transfer_to(p)
        child.update({"tuition_transfer_to": transfer_to} if transfer_to else {})
    return child


def admit_people(doc: Dict, people: Dict[str, Dict], couple: set,
                horizon_end: Optional[int]) -> tuple:
    """Partition the document's people into the ones the engine can hold.

    Returns ``(child_ids, extra_adult_ids)`` -- the dependent descendants
    admitted through the N-child seam, and the additional ACCUMULATING adults
    admitted through the N-adult compute. Anybody left over is REFUSED LOUDLY
    (``ContractAdaptationError``), never silently truncated.

    ``couple`` is the selected primary+spouse pair; ``horizon_end`` is
    ``_horizon_end_year``'s answer (None when the horizon does not date
    against the primary, in which case an extra adult cannot be proven a pure
    accumulator and every one of them is refused)."""
    # Issue #698 (Step 8 of #643): admit additional GENERATIONS beyond the one
    # couple + their direct children -- but ONLY the people the post-Steps-1-7
    # engine can hold without silently dropping them. Every extra person the
    # dependent-CHILD seam represents faithfully (own accounts seeded+grown,
    # income taxed individually -- epic #841 + Step 6) is admitted as a child,
    # of any generation (a grandchild reaches _map_child exactly like a direct
    # child). An extra person carrying an ADULT-only fact the child seam drops
    # -- a spouse pairing (a SECOND couple), retirement benefits, a future
    # salary, an independent retirement age -- is still REFUSED LOUDLY, because
    # holding them needs the N-adult compute Step 5 deferred (the two-slot
    # WorkingState / simulate_year_pure signature; SimState.initial seeds only
    # primary+spouse) -- tracked by #706 / Step 9. This keeps the couple-only
    # golden mapping byte-identical while ending the blanket refusal of every
    # extra generation. See _needs_adult_compute for the precise field list.
    descendants = _parent_of_targets(doc)
    admissible_children = sorted(
        pid for pid in people
        if pid not in couple
        and pid in descendants
        and not _needs_adult_compute(doc, pid, people[pid])
    )
    # Issue #899 (part a): admit an ADDITIONAL adult (a person carrying an
    # adult-only fact the child seam would drop) into the newly-uncapped
    # N-adult compute -- but ONLY when they are a pure ACCUMULATOR across the
    # whole horizon (_is_pure_accumulator). The compute uncap is defined only
    # for adults who never decumulate; a retired / benefit-drawing / soon-to-
    # retire extra adult needs the spending-target + mortality model tracked in
    # #901, so they stay refused (below) rather than admitted-and-mishandled
    # (DP#32). Requires a horizon that dates against the primary; without one an
    # accumulator cannot be proven and every extra adult is refused.
    admissible_adults = sorted(
        pid for pid in set(people) - couple - set(admissible_children)
        if horizon_end is not None
        and _is_pure_accumulator(doc, pid, people[pid], horizon_end)
    )
    refused = sorted(
        set(people) - couple - set(admissible_children) - set(admissible_adults))
    if refused:
        needs_adult = [pid for pid in refused
                       if _needs_adult_compute(doc, pid, people[pid])]
        not_dependents = [pid for pid in refused if pid not in needs_adult]
        raise ContractAdaptationError(
            f"This document describes people the engine cannot yet place "
            f"without silently dropping them (issue #698 / #643 Step 8; "
            f"#899 admits additional ACCUMULATING adults). "
            + (f"Additional ADULTS {needs_adult} are retired, drawing a "
               f"CPP/OAS/pension benefit, hold a decumulation account, or reach "
               f"retirement age within the horizon -- so they DECUMULATE, which "
               f"the N-adult compute uncap (#899, accumulators only) deliberately "
               f"does NOT model. A non-primary adult's spending target and a "
               f"mid-horizon mortality/estate lifecycle are the genuine modeling "
               f"gaps tracked by **#901**; until it lands, admitting them would "
               f"either freeze their accounts or invent a drawdown target (DP#32). "
               if needs_adult else "")
            + (f"People {not_dependents} are neither the selected couple nor a "
               f"dependent descendant (no parent_of relationship names them), so "
               f"the child seam has no place for them either. " if not_dependents else "")
            + f"Refusing rather than silently truncating a generation."
        )
    child_ids = admissible_children
    extra_adult_ids = admissible_adults
    return child_ids, extra_adult_ids


def map_members(doc: Dict, primary_id: str, spouse_id: Optional[str],
                extra_adult_ids: List[str],
                registered_balances: Dict[str, Dict[str, float]]) -> List[Dict]:
    """The household's ADULT members, in the order the engine's N-adult seams
    iterate them: the primary, the spouse (when there is one), then each
    admitted additional accumulating adult in declared order.

    Every one of them must have a dateable birth date (issue #100, DP#28/#32):
    without a DOB the retirement gate would silently map the member as 'never
    retires' -- zero CPP/OAS/pension, indistinguishable from a correctly
    ineligible member. Refused loudly here rather than fabricated."""
    # Issue #100 (DP#28/DP#32): a person ADMITTED as a simulated ADULT member
    # (primary / spouse / accumulating extra adult) must have a dateable birth
    # date. Without a DOB the engine cannot date retirement eligibility, CPP/
    # OAS claim ages, or the RRIF/age-71 lifecycle -- and the retirement gate
    # would silently map such a member as 'never retires' (zero CPP/OAS/
    # pension), indistinguishable from a correctly-ineligible member (the
    # silent-zero false green this codebase exists to eliminate). The schema
    # sanctions birth_date: null ONLY for a deceased ancestor, who is never
    # admitted as a member (the accumulator/extra-adult boundary refuses
    # anyone it cannot date). Refuse loudly HERE, at the contract boundary,
    # rather than let the gate fabricate a never-retiring member.
    for _admitted_id in [primary_id] + ([spouse_id] if spouse_id else []) + extra_adult_ids:
        _admitted = _people_by_id(doc).get(_admitted_id, {})
        if not _admitted.get("birth_date"):
            raise ContractAdaptationError(
                f"Person {_admitted_id!r} is admitted as a simulated ADULT "
                f"member but has no birth_date (DP#28/#100). The engine must "
                f"date retirement eligibility, CPP/OAS claim ages, and the "
                f"RRIF/age-71 lifecycle from a birth date; a null birth_date is "
                f"sanctioned by the schema only for a DECEASED ANCESTOR, who is "
                f"never admitted as a member. Without a DOB, mapping this member "
                f"as 'never retires' (zero CPP/OAS/pension) would be a "
                f"silent-zero gate -- refusing rather than inventing one."
            )

    members = [_map_member(doc, primary_id, "primary", registered_balances)]
    if spouse_id:
        members.append(_map_member(doc, spouse_id, "spouse", registered_balances))
    # Issue #899 (part a): each admitted accumulating adult becomes a full ADULT
    # member, keyed by its stable person_id as its role so the N-adult seams
    # (config.adults(), the per-adult tax loop, the per-adult account stores)
    # iterate it in declared order after the primary couple.
    for xid in extra_adult_ids:
        members.append(_map_member(doc, xid, xid, registered_balances))

    # Issue #141: validate every declared superficial-loss disposition
    # against the household that declared it -- loudly, at load (DP#32):
    # an unresolvable `acquired_by`, or a declaration on someone who is NOT
    # a simulated adult member (a child, an outsider), has no tax seam the
    # engine could price, and silently dropping it would price a denial
    # that never happens (or hide one that does).
    admitted = {m["id"] for m in members}
    people = _people_by_id(doc)
    for pid, person in people.items():
        events = person.get("superficial_losses")
        if not events:
            continue
        if pid not in admitted:
            raise ContractAdaptationError(
                f"Person {pid!r} declares superficial_losses but is not a "
                f"simulated adult member of this household. The ITA "
                f"s.53(1)(c)/s.54 denial has no tax seam to price through a "
                f"non-member (a child's or outsider's acquisition is not the "
                f"seller's affiliated-person attribution this engine models). "
                f"Move the declaration to the disposing member or drop it "
                f"(issue #141)."
            )
        for e in events:
            acquirer = e.get("acquired_by")
            if acquirer not in admitted:
                raise ContractAdaptationError(
                    f"Person {pid!r} declares a superficial-loss disposition "
                    f"acquired_by {acquirer!r}, which is not a simulated "
                    f"adult member of this household. s.54 affiliation spans "
                    f"the household (the spouse acquiring IS attribution); "
                    f"declare only acquisitions by a simulated member "
                    f"(issue #141)."
                )
            amount = e.get("loss_amount")
            if not isinstance(amount, (int, float)) or amount <= 0.0:
                raise ContractAdaptationError(
                    f"Person {pid!r} declares a superficial-loss disposition "
                    f"with loss_amount {amount!r}. A denial "
                    f"needs a positive pre-inclusion loss magnitude -- there "
                    f"is nothing to deny otherwise (DP#32)."
                )

    return members
