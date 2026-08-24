"""The ``decisions`` namespace: the questions the household is asking.

A decision is neither a fact nor a belief -- it is a lever the optimizer
sweeps. This module maps every one of them:

* ``decisions.mortgage`` -- renewal RATE options, cash-out REFINANCE options,
  and the charge STRUCTURE options (#687/#1075), each validated against the
  registered charge at load time so an impossible structure is refused
  immediately, not only if a sweep happens to reach it.
* ``decisions.income`` -- dated income shocks, together with the
  employment-contract terms that bound them (#767): a non-competition covenant
  clamps a recovery dated inside the window, and contractual
  pay-in-lieu-of-notice becomes a paid employment segment at termination.
* ``decisions.contribution_strategy`` -- the household's own authored
  allocation strategies, so the ranked table ranks what they wrote down (#713).
* ``decisions.resp_action`` and ``decisions.objective`` -- the RESP wind-down
  scenarios, and the objective the optimizer ranks under (#862), validated
  against ``objective.OBJECTIVES`` at this single ingestion boundary.
* ``decisions.borrow_to_invest`` -- the one-shot leverage decision (#1036),
  resolved against the declared kind=heloc facility and refused loudly on any
  source/amount/target the engine cannot honour.
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError
from contract_people import _people_by_id

logger = logging.getLogger(__name__)


# Issue #767: the employment-contract terms that bound when income can
# recover after a job loss. ``_REEMPLOYMENT_KINDS`` are the income kinds that
# represent re-employment (being paid to work in one's specialty) -- a
# non-competition covenant bars exactly these, NOT continued EI (``ei`` is
# a statutory benefit, not re-employment, and a job-loss schedule's EI
# segment legitimately runs during the non-compete window).
_REEMPLOYMENT_KINDS = {"employment", "self_employment"}


def _non_compete_months(inc: Dict) -> Optional[int]:
    """Issue #767: the non-compete duration (months) on an employment income,
    or None when no non-compete is declared.

    The schema ($defs/employment_contract.non_compete) guarantees that when
    the ``non_compete`` object is present all three of months/scope/geography
    are present -- a partial declaration (scope but no months) is rejected at
    validation, before this function ever sees the income. So reading
    ``['months']`` here is safe; there is no ``.get('months')``-with-default
    to silently invent a duration (DP#32).
    """
    emp = inc.get("employment")
    if not emp:
        return None
    nc = emp.get("non_compete")
    return nc["months"] if nc else None


def _notice_days(inc: Dict) -> int:
    """Issue #767: the contractual pay-in-lieu-of-notice entitlement (days)
    on an employment income, or 0 when none is declared.

    The schema ($defs/employment_contract) makes ``notice_days`` a required
    integer >= 0 on every ``employment`` block, so 0 is the EXPLICIT spelling
    of "no contractual notice" -- never an absent-key default (DP#32). An
    income with no ``employment`` block has no notice entitlement either;
    0 is returned in both cases.
    """
    emp = inc.get("employment")
    return emp.get("notice_days", 0) if emp else 0


def _add_months(d: _date, months: int) -> _date:
    """Pure: return ``d`` shifted forward by ``months`` whole months.

    ``dateutil`` is not a dependency; this is the one calendar-arithmetic
    helper #767 needs (non-compete expiry = termination + N months). It
    handles year rollover without ever producing an invalid month.
    """
    return d.replace(
        year=d.year + (d.month - 1 + months) // 12,
        month=(d.month - 1 + months) % 12 + 1,
    )


def _apply_notice_segments(
    overrides: List[Dict], people_by_id: Dict[str, Dict],
    person_income_ids: Dict[str, str], scenario_id: str,
) -> List[Dict]:
    """Issue #767: model contractual pay-in-lieu-of-notice as a paid
    employment-income segment at the start of a job-loss schedule.

    For each ``income_id`` whose employment income declares
    ``employment.notice_days > 0`` and whose scenario has a job-loss shock
    (an override whose ``kind`` is NOT a re-employment kind -- i.e. ``ei`` or
    ``other``, the income that REPLACES employment after termination), the
    shock's ``from`` is the termination date. Notice is pay-in-lieu: the
    person receives their BASE employment salary for ``notice_days`` after
    termination, BEFORE the replacement income begins. This inserts a short
    ``kind=employment`` segment ``[termination, termination + notice_days)``
    at the base salary and shifts the shock's ``from`` forward by
    ``notice_days`` so the two do not overlap (``_income_components_for_year``
    rejects overlapping segments -- #674).

    This extends runway: the household has full employment income
    (RRSP-room-accruing, kind=employment) during the notice window instead
    of the lower EI/replacement amount.

    Call order: ``_apply_non_compete_to_overrides`` runs FIRST (on the
    un-shifted overrides, so its termination date is the true employment-end
    date), then this function runs on the clamped result. The non-compete
    clock starts at the real termination, not at the end of notice.

    Returns a NEW list; the caller's overrides are not mutated. Logs nothing
    -- a notice segment is the contract's plain meaning, not a correction.
    """
    by_income: Dict[str, List[Dict]] = {}
    for ov in overrides:
        by_income.setdefault(ov["income_id"], []).append(ov)

    result: List[Dict] = []
    for ov in overrides:
        iid = ov["income_id"]
        group = sorted(by_income[iid], key=lambda o: o["from"])
        shock = group[0] if group else None
        owner_id = person_income_ids.get(iid)
        owner = people_by_id.get(owner_id, {}) if owner_id else {}
        inc = next((i for i in owner.get("incomes", []) if i["id"] == iid), None)
        notice = _notice_days(inc) if inc else 0
        # Only the SHOCK (the earliest, non-re-employment override) gets a
        # notice segment prepended; a recovery/raise override is not a
        # termination and has no notice entitlement to model here.
        is_shock = (shock is ov and ov["kind"] not in _REEMPLOYMENT_KINDS)
        if notice > 0 and is_shock:
            termination = _date.fromisoformat(ov["from"])
            notice_end = _date.fromordinal(termination.toordinal() + notice)
            base_salary = inc["amount"] if inc else ov["amount"]
            notice_seg = {
                "income_id": iid,
                "kind": "employment",
                "amount": base_salary,
                "from": ov["from"],
                "to": notice_end.isoformat(),
            }
            result.append(notice_seg)
            shifted = dict(ov)
            shifted["from"] = notice_end.isoformat()
            result.append(shifted)
        else:
            result.append(ov)
    return result


def _apply_non_compete_to_overrides(
    overrides: List[Dict], people_by_id: Dict[str, Dict],
    person_income_ids: Dict[str, str], scenario_id: str,
) -> List[Dict]:
    """Issue #767: enforce a declared non-competition covenant on the dated
    income overrides for one income_scenario.

    A non-compete bars re-employment in the person's own specialty for
    ``months`` after the employment ends. For each ``income_id`` that
    targets an employment income carrying ``employment.non_compete`` with
    ``months > 0``:

    * the EARLIEST override is the job-loss shock -- its ``from`` is the
      termination date, and ``earliest = termination + non_compete.months``;
    * any LATER override whose ``kind`` is a re-employment kind
      (``_REEMPLOYMENT_KINDS``) and whose ``from`` precedes ``earliest`` is
      a recovery dated before the contract allows re-employment -- it is
      CLAMPED forward to ``earliest``, and a loud warning is emitted;
    * the shock's ``to`` is EXTENDED to ``earliest`` when it would otherwise
      end before the (clamped) recovery begins. Without this, the gap
      between the shock's end and the recovery's start would revert to the
      BASE employment salary (``_income_components_for_year``'s uncovered-
      days fallback) -- i.e. the engine would silently model re-employment
      at full salary DURING the non-compete window, which is exactly the
      runway overstatement #767 exists to prevent (DP#32: the engine must
      not guess re-employment the contract forbids).

    An income with no ``employment`` block (or no ``non_compete``, or
    ``months == 0``) is untouched -- absence is modelled, not guessed
    (backward compatible). A recovery dated OUTSIDE the window is untouched.

    Returns a NEW list; the caller's overrides are not mutated. LOGS the
    clamp -- the issue asks for a warning, and a silent clamp would itself
    be a confident-but-wrong number.
    """
    by_income: Dict[str, List[Dict]] = {}
    for ov in overrides:
        by_income.setdefault(ov["income_id"], []).append(ov)

    # First pass: the earliest-allowed re-employment date per income_id, so
    # the shock's `to` can be extended in the same pass that clamps recovery.
    # None when there is no non-compete, no shock+recovery, or no recovery in
    # a re-employment kind (continued EI alone does not trigger the covenant).
    earliest_by_iid: Dict[str, Optional[_date]] = {}
    nc_months_by_iid: Dict[str, int] = {}
    for iid, group in by_income.items():
        ordered = sorted(group, key=lambda o: o["from"])
        if len(ordered) < 2:
            continue
        shock = ordered[0]
        owner_id = person_income_ids.get(iid)
        owner = people_by_id.get(owner_id, {}) if owner_id else {}
        inc = next((i for i in owner.get("incomes", []) if i["id"] == iid), None)
        nc_months = _non_compete_months(inc) if inc else None
        if not nc_months:
            continue
        recovery = next((o for o in ordered[1:]
                         if o["kind"] in _REEMPLOYMENT_KINDS), None)
        if recovery is None:
            continue
        termination = _date.fromisoformat(shock["from"])
        earliest_by_iid[iid] = _add_months(termination, nc_months)
        nc_months_by_iid[iid] = nc_months

    result: List[Dict] = []
    for ov in overrides:
        iid = ov["income_id"]
        earliest = earliest_by_iid.get(iid)
        if earliest is None:
            result.append(ov)
            continue
        ordered = sorted(by_income[iid], key=lambda o: o["from"])
        shock = ordered[0]
        is_shock = (ov is shock)
        is_recovery = (not is_shock) and (ov["kind"] in _REEMPLOYMENT_KINDS)
        if is_recovery:
            recovery_from = _date.fromisoformat(ov["from"])
            if recovery_from < earliest:
                logger.warning(
                    "decisions.income[] scenario %r: recovery income_override "
                    "for income_id %r is dated %s, inside the declared %d-month "
                    "non-competition covenant (termination %s, earliest "
                    "re-employment %s). Clamping the recovery `from` to %s so "
                    "runway is not overstated (issue #767).",
                    scenario_id, iid, ov["from"], nc_months_by_iid[iid],
                    shock["from"], earliest.isoformat(), earliest.isoformat(),
                )
                clamped = dict(ov)
                clamped["from"] = earliest.isoformat()
                result.append(clamped)
            else:
                result.append(ov)
        elif is_shock:
            # Extend the shock's `to` to cover the non-compete window so the
            # uncovered-days fallback does not silently revert to base salary
            # during the covenant window. Only extend FORWARD (never shrink);
            # an open-ended shock (to=None) already covers everything.
            shock_to = (_date.fromisoformat(shock["to"])
                        if shock.get("to") else None)
            if shock_to is not None and shock_to < earliest:
                extended = dict(ov)
                extended["to"] = earliest.isoformat()
                result.append(extended)
            else:
                result.append(ov)
        else:
            result.append(ov)
    return result


def map_mortgage_decisions(doc: Dict, prop_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``decisions.mortgage.*`` -> the renewal / refinance / structure levers.

    Returns the ``scenarios.refinance`` list, because a refinance option is a
    CASH-OUT decision and that block's consumer is the one that reads
    ``cash_out``. The levers that are properties of the CHARGE rather than
    scenarios over it -- ``renewal_options``, ``refinance_amortization_years``,
    ``refinance_advance_deductible_non_reg`` and ``structure_options`` -- are
    written onto ``prop_cfg`` in place."""
    # ── decisions.mortgage.* -- FIXED in Phase 2c (found by Phase 2b's schema
    # coverage rewrite). These two options are NOT the same kind of thing, and
    # Phase 1 mapped both onto the internal `property.{refinance,renewal}_options`
    # keys as though they were:
    #
    #   - A contract `renewal_option` is a RATE option {id,label,rate,type,
    #     term_years}. The internal consumer (scenario_discovery.py's mortgage
    #     rate-scenario builder, optimize.py's discover_rate_anchors) wants
    #     exactly that -- except it keys the human name off `name`, not `label`.
    #     Mapped, with the rename, so it actually lands.
    #   - A contract `refinance_option` is a CASH-OUT option {id,label,cash_out,
    #     ltv}. It carries no rate at all. Feeding it to a RATE-scenario builder
    #     meant every refinance option rendered as "Refinance: Unknown option" at
    #     a hardcoded 5%/variable/5yr default, and its real content (`cash_out`)
    #     was never read by anyone. Its actual consumer is
    #     scenario_discovery._convert_refinance_scenarios, which reads
    #     `scenarios.refinance[].cash_out` -- so that is where it goes now.
    mortgage_decisions = doc["decisions"]["mortgage"]  # both schema-required
    refinance_options = mortgage_decisions["refinance_options"]
    renewal_options = mortgage_decisions["renewal_options"]
    if renewal_options:
        prop_cfg["renewal_options"] = [
            {"name": o["label"], "rate": o["rate"], "type": o["type"],
             "term_years": o["term_years"]}
            for o in renewal_options
        ]
    refinance_scenarios = [
        {"id": o["id"], "label": o["label"], "cash_out": o["cash_out"]}
        for o in refinance_options
    ]
    # issue #655: a cash-out refinance is a NEW LOAN, re-amortized over its
    # own declared term -- apply_ltv_overlay/apply_overlay read this as
    # property.refinance_amortization_years instead of silently inheriting
    # the incumbent mortgage's remaining amortization_years. Every declared
    # refinance_option carries its own amortization_years, but
    # apply_ltv_overlay's ``ltv`` is a continuous sweep target, not a
    # specific chosen option (the same limitation already noted for `.ltv`
    # above, #601/#595) -- so this takes the first option's value as the
    # household's stated new-loan term. A contract offering refinance
    # options with genuinely different amortizations across cash-out levels
    # is not distinguishable through this single scalar; that is real,
    # separate follow-up work, not silently swept under DP#32.
    if refinance_options:
        prop_cfg["refinance_amortization_years"] = refinance_options[0]["amortization_years"]

    # Issue #792: a declared split of the refinance advance between the
    # DEDUCTIBLE non-reg account and registered accounts. Borrowed money
    # routed to non-reg establishes s.20(1)(c) interest deductibility at the
    # refinance (irreversibly); borrowed money put into RRSP/TFSA is
    # non-deductible forever (s.18(11)). The household -- not the engine --
    # makes this call, so it is a declared contract lever (DP#2), carried as
    # the advance_split.deductible_non_reg of the refinance option that
    # declares one (the household annotates the cash-out option they are
    # actually considering -- not necessarily the first option, which may be
    # a no-refinance baseline with cash_out 0 and no split to declare).
    # The LTV-exploration path sweeps a continuous ltv rather than a specific
    # chosen option, so a single scalar is all one option's worth of split
    # can be -- the first declared advance_split is the household's stated
    # intent for the advance they are weighing. Absent on every option means
    # "no declared split" -- the engine keeps today's internal optimization
    # (fill registered first, non-reg gets the remainder). A declared 0 is a
    # real choice and is carried as 0 (DP#32: 0 is a value, not a fallback --
    # only absence is absence). The mapped key
    # property.refinance_advance_deductible_non_reg is read by
    # SimulationConfig.from_dict onto the config field that
    # StrategyEngine.fill_room honors (the cash-out / LTV optimizer's year-0
    # lump-sum waterfall), so the leaf provably reaches the engine.
    if refinance_options:
        split_option = next(
            (o for o in refinance_options if o.get("advance_split") is not None),
            None,
        )
        if split_option is not None:
            prop_cfg["refinance_advance_deductible_non_reg"] = (
                split_option["advance_split"]["deductible_non_reg"]
            )

    # ── decisions.mortgage.structure_options -- issue #687. A household
    # facing a refinance/renewal may be choosing between genuinely different
    # STRUCTURES against the SAME registered charge (all-mortgage vs.
    # readvanceable vs. mortgage-plus-undrawn-line) -- this is the ability
    # to ASK the question at all; #664/#655/#681 already built the charge
    # mechanics these structures are made of. Optional (absent means the
    # household has not declared this as an open decision -- the engine
    # simply runs the declared liabilities[] as-is, no structure sweep).
    # Issue #1075: a structure may alternatively declare ``tranches`` (the
    # 3-tranche readvanceable form -- house >= a minimum, deductible
    # investment mortgage, readvanceable line) -- an additive opt-in over
    # the #687 share form; the schema's oneOf keeps the two mutually
    # exclusive.
    structure_options = mortgage_decisions.get("structure_options", [])
    if structure_options:
        # Both moved out of simulation_config when the config was split: the
        # structure overlay to property_structure, the charge refusal to
        # charge_limits (DP#9 -- re-pointed, not re-exported).
        from property_structure import apply_structure_overlay
        from charge_limits import ChargeLimitExceededError
        prop_cfg["structure_options"] = []
        for opt in structure_options:
            readvanceable = opt.get("readvanceable", False)
            revolving_rate = opt.get("revolving_rate")
            revolving_rate_type = opt.get("revolving_rate_type")
            if "revolving_share" in opt:
                # ── the #687 share form (byte-identical mapping) ──
                revolving_share = opt["revolving_share"]
                # DP#32/#654: a structure that CAN carry a revolving balance --
                # today (revolving_share > 0) or only later (readvanceable: the
                # readvance mechanism, #664/#681, grows the line from $0 as
                # principal is repaid) -- must be priced. Silently deriving its
                # carrying cost from the mortgage's own rate is exactly the bug
                # #654 exists to prevent; refused loudly here instead.
                if (revolving_share > 0 or readvanceable) and (
                        revolving_rate is None or revolving_rate_type is None):
                    raise ContractAdaptationError(
                        f"decisions.mortgage.structure_options[id={opt['id']!r}] "
                        f"({opt['label']!r}) carries a revolving component "
                        f"(revolving_share={revolving_share:.0%}"
                        f"{', readvanceable' if readvanceable else ''}) but "
                        f"declares no revolving_rate/revolving_rate_type. A "
                        f"line that can draw -- directly today, or later via "
                        f"readvance -- must be priced (#654/#687); it is never "
                        f"derived from the mortgage's own rate."
                    )
                structure_entry = {
                    "id": opt["id"], "label": opt["label"],
                    "revolving_share": revolving_share,
                    "readvanceable": readvanceable,
                    "revolving_rate": revolving_rate,
                    "revolving_rate_type": revolving_rate_type,
                }
            else:
                # ── the #1075 tranche form (additive opt-in) ──
                structure_entry = {
                    "id": opt["id"], "label": opt["label"],
                    "readvanceable": readvanceable,
                    "revolving_rate": revolving_rate,
                    "revolving_rate_type": revolving_rate_type,
                    "tranches": [dict(t) for t in opt["tranches"]],
                }
            # Fail fast at contract-load time (DP#32): the SAME charge/
            # revolving-cap enforcement `apply_structure_overlay` repeats
            # dynamically (see its docstring) against whatever the combined
            # secured debt is when a sweep actually applies this structure
            # (e.g. after a cash-out overlay has grown it further) -- run
            # here too, against the BASELINE declared mortgage + HELOC, so
            # an impossible structure is refused immediately, not only if
            # the optimizer happens to sweep it. Result discarded -- this
            # call is for its refusal side effect, not for a value. For a
            # tranches-declared structure the tranche machinery validates
            # the SPEC (overlapping kinds, a house floor above the charge,
            # an unpriced line) and applies the binding minimum point; its
            # ValueError refusals surface as ContractAdaptationError here.
            try:
                apply_structure_overlay(prop_cfg, structure_entry)
            except ChargeLimitExceededError:
                # A charge/cap breach propagates unchanged -- the #687 tests
                # and callers depend on the specific type.
                raise
            except ValueError as exc:
                raise ContractAdaptationError(
                    f"decisions.mortgage.structure_options[id={opt['id']!r}] "
                    f"({opt['label']!r}): {exc}"
                ) from exc
            prop_cfg["structure_options"].append(structure_entry)
    return refinance_scenarios


def map_income_scenarios(doc: Dict, primary_id: str, spouse_id: Optional[str]) -> List[Dict]:
    """``decisions.income[]`` -> the dated income-shock scenarios.

    Each scenario's overrides pass through the employment-contract terms first
    (issue #767): a non-competition covenant clamps a re-employment override
    dated inside the window forward (with a loud warning), then contractual
    pay-in-lieu-of-notice is modelled as a paid employment segment at
    termination. An override naming an income neither spouse declares is
    refused loudly rather than silently vanishing (#674)."""
    income_scenarios = []
    people_by_id = _people_by_id(doc)
    for sc in doc["decisions"]["income"]:  # schema-required (may be [])
        person_income_ids = {
            inc["id"]: pid
            for pid, p in people_by_id.items()
            for inc in p.get("incomes", [])
        }
        # Issue #767: enforce the employment-contract terms BEFORE mapping --
        # (1) a declared non-competition covenant clamps a re-employment
        #     override dated inside the window forward to the earliest allowed
        #     date (loud warning), so runway is not overstated;
        # (2) contractual notice_days is modelled as a paid employment-income
        #     segment at termination, extending runway.
        # Non-compete runs first so its termination date is the true employment-
        # end date (notice shifts the shock's `from` but is not itself a
        # termination). Done here because both the override windows and the base
        # income's employment block are in scope at the contract-mapping layer.
        clamped = _apply_non_compete_to_overrides(
            sc["overrides"], people_by_id, person_income_ids, sc["id"])
        clamped_overrides = _apply_notice_segments(
            clamped, people_by_id, person_income_ids, sc["id"])
        sc_members = []
        for ov in clamped_overrides:
            owner_id = person_income_ids.get(ov["income_id"])
            # Issue #674: an override whose income_id belongs to neither the
            # primary nor the spouse (a typo'd id, or a child's income --
            # income_override has no mechanism to reach a child today) used
            # to fall through both branches and vanish with no error. A
            # scenario a household explicitly declared is not optional --
            # silently dropping it is the exact "parsed, mapped, then never
            # passed" failure #665 was filed over (DP#32).
            if owner_id not in (primary_id, spouse_id):
                raise ContractAdaptationError(
                    f"decisions.income[] scenario {sc['id']!r} overrides "
                    f"income_id {ov['income_id']!r}, which is not declared "
                    f"on the primary or the spouse (owner: {owner_id!r}). "
                    f"income_override can only reach the primary/spouse "
                    f"today. Declared income_ids: "
                    f"{sorted(person_income_ids)}."
                )
            role = "primary" if owner_id == primary_id else "spouse"
            # Issue #674 (Gap 1 + Gap 2): kind/from/to travel with the
            # amount now, not just a flat gross_income overwrite -- a
            # duration-bounded, kind-classified income_override is what lets
            # the engine tell an EI-shaped job-loss gap from a permanent
            # salary cut (see scenario_discovery._convert_income_scenarios
            # and simulation.py's _income_components_for_year).
            sc_members.append({
                "role": role,
                "gross_income": ov["amount"],
                "kind": ov["kind"],
                "from": ov["from"],
                "to": ov["to"],
                # Issue #980 (T2125): the override's optional professional-
                # expense total travels with the gross amount so the engine
                # derives NET business income (gross - expenses) for a
                # self_employment override. None when the override declares
                # no expenses (DP#32: the engine taxes the full gross amount,
                # byte-identical to pre-#980). Read with .get, NOT _require --
                # expenses_annual is OPTIONAL (only a self_employment earner
                # with deductible expenses declares it).
                "expenses_annual": ov.get("expenses_annual"),
            })
        income_scenarios.append({"id": sc["id"], "label": sc["label"], "members": sc_members})
    return income_scenarios


def map_resp_action_scenarios(doc: Dict) -> List[Dict[str, str]]:
    """``decisions.resp_action[]`` -> the RESP wind-down scenarios."""
    resp_action_scenarios = [
        {"id": a["id"], "label": a["label"]}
        for a in doc["decisions"]["resp_action"]  # schema-required (may be [])
    ]
    return resp_action_scenarios


def map_contribution_strategies(doc: Dict) -> tuple:
    """``decisions.contribution_strategy[]`` ->
    ``(strategies_cfg, declared_targets)``.

    ``strategies_cfg`` is the household's OWN authored strategies, in the shape
    ``AllocationStrategy.from_dict`` already reads, so they reach the
    optimizer's search space (issue #713) instead of the ranked table ranking
    the engine's two built-ins.

    ``declared_targets`` is the SET of declared ``deduct_later_bracket_target``
    values. The engine has exactly ONE bracket-fill target, read off
    ``assumptions.deduct_later_bracket_target``, so the caller lands the single
    declared value there -- and two strategies asking for DIFFERENT targets is
    refused here rather than silently picked between (DP#32)."""
    # ── Issue #713: the household's OWN authored contribution strategies.
    #
    # The block was parsed by the schema and then dropped on the floor: nothing
    # mapped it, so `decisions.contribution_strategy[]` never reached the
    # optimizer, and the ranked table a user got back ranked the engine's two
    # built-in strategies (`balanced`/`rrsp_max`) -- never the ones they wrote
    # down. The optimizer has ALWAYS had the hook for this
    # (`discover_strategies(custom_strategies=...)`, whose own docstring cites
    # DP#2, "configuration belongs in input, not in code"); both call sites in
    # optimize.py simply never passed anything to it.
    #
    # Mapped into the shape AllocationStrategy.from_dict already reads, so the
    # contract's spelling of each lever meets the engine's:
    #   use_smith                   -> prioritize_readvanceable  (the
    #                                  readvanceable-priority MECHANISM, not a
    #                                  branded product name -- DP#7)
    #   deduct_later_bracket_target -> bracket_target
    strategies_cfg: Dict[str, Any] = {}
    for strat in doc["decisions"]["contribution_strategy"]:  # schema-required (may be [])
        alloc = strat["allocation"]  # schema-required, and every pct is required
        mapped: Dict[str, Any] = {
            "name": strat["label"],
            "rrsp_pct": alloc["rrsp_pct"],
            "spousal_rrsp_pct": alloc["spousal_rrsp_pct"],
            "tfsa_pct": alloc["tfsa_pct"],
            "fhsa_pct": alloc["fhsa_pct"],
            "resp_pct": alloc["resp_pct"],
            "non_reg_pct": alloc["non_reg_pct"],
            "prioritize_readvanceable": strat["use_smith"],
            "deduct_later": strat["deduct_later"],
        }
        # DP#32/DP#13: deduct_later_bracket_target is nullable (null when
        # deduct_later=false -- the schema says so). Explicit presence test,
        # never a truthiness coercion: a declared target of 0 is a real target,
        # while an ABSENT one must fall back to the engine's default rather
        # than be forced to None, which would poison the bracket-fill maths.
        if strat["deduct_later_bracket_target"] is not None:
            mapped["bracket_target"] = strat["deduct_later_bracket_target"]
        strategies_cfg[strat["id"]] = mapped

    # The bracket-fill target is the ONE lever of this block the engine reads
    # from somewhere other than the strategy: the deduct-later rule prices it
    # off SimulationConfig.deduct_later_bracket_target (simulation_rules.py's
    # `bracket_target=ctx.config.deduct_later_bracket_target`, and
    # `should_deduct_later`), which from_dict populates from
    # assumptions.deduct_later_bracket_target. The contract collapsed the two
    # legacy spellings of this fact into the one decision that parametrizes it
    # (#595C -- the schema's own description says so), so the mapping has to
    # land it on the key the engine actually reads, not only inside the
    # strategy dict, or the target would be "mapped" and still do nothing.
    declared_targets = {
        strat["deduct_later_bracket_target"]
        for strat in doc["decisions"]["contribution_strategy"]
        if strat["deduct_later"] and strat["deduct_later_bracket_target"] is not None
    }
    if len(declared_targets) > 1:
        # The engine has ONE bracket-fill target, and two strategies asking for
        # different ones cannot both be honoured. Picking one silently is the
        # exact defect this module exists to prevent (DP#32) -- so refuse.
        raise ContractAdaptationError(
            f"decisions.contribution_strategy[] declares {len(declared_targets)} "
            f"different deduct_later_bracket_target values "
            f"({sorted(declared_targets)}), but the engine has a single "
            f"bracket-fill target. Declare one target, or model the alternatives "
            f"as separate runs -- this mapping will not pick one for you."
        )
    return strategies_cfg, declared_targets


def map_declared_objective(doc: Dict) -> Optional[str]:
    """``decisions.objective`` -> the declared optimization objective, or None
    when the household declares none (the default ``max_net_benefit``)."""
    # Issue #862 (DP#22/DP#5/DP#32): the household's declared optimization
    # objective. `decisions.objective` is OPTIONAL -- absent means the default
    # `max_net_benefit`, so a contract that never declares one is a pure no-op
    # (the golden household declares none; its ranking objective is unchanged).
    # When declared, the name is validated against objective.OBJECTIVES HERE,
    # at the single ingestion boundary, so a typo is refused loudly (naming the
    # bad value and listing the valid names) rather than silently scoring the
    # run under the wrong objective three layers deep. The optimizer ranks, it
    # doesn't choose (DP#22); this is where the household chooses.
    declared_objective = doc["decisions"].get("objective")
    if declared_objective is not None:
        from objective import OBJECTIVES
        if declared_objective not in OBJECTIVES:
            raise ContractAdaptationError(
                f"decisions.objective = {declared_objective!r} is not a known "
                f"optimization objective. The optimizer ranks strategies under a "
                f"declared objective (DP#22); an unknown name is refused loudly "
                f"rather than silently scoring the run under the wrong one "
                f"(DP#32). Valid objectives: {sorted(OBJECTIVES)}."
            )
    return declared_objective


def map_borrow_to_invest(doc: Dict, heloc: Optional[Dict]) -> List[Dict[str, Any]]:
    """``decisions.borrow_to_invest[]`` -> ``legacy['borrow_to_invest_options']``.

    Issue #1036: a first-class, swept, objective-ranked one-shot
    borrow-to-invest decision (draw $X against a declared HELOC at year 0,
    invest the proceeds in non-reg, deduct the interest under ITA s.20(1)(c)).
    Mirrors ``decisions.mortgage.structure_options[]`` /
    ``refinance_options[]``: each declared option is a rung on the amount
    ladder, and the optimizer also runs the implicit amount=0 (no-draw)
    baseline so 'do nothing' is always the frame of reference (DP#33: a
    declaration is a lens, not a blindfold -- the sweep is not replaced by the
    declared set, it is annotated by it). The draw reuses the year-0
    margin-draw machinery (initial_state_for_run); the interest is priced and
    deducted by the existing drawn-margin rules, traced 100% investment. No
    readvanceable facility is required -- a mortgage-free household with a
    HELOC can express leverage this way.

    DP#32 boundary refusals (a partial/unsupported declaration must FAIL
    LOUDLY, never silently coerce to the supported value):

    * ``source`` must resolve to a declared kind=heloc liability. A typo or a
      reference to a non-heloc liability (a margin account, an unsecured
      line_of_credit) is refused loudly -- the engine draws against a
      property-secured HELOC in this slice; other sources are follow-up.
    * ``amount`` must be > 0 and <= the source HELOC's limit. A draw larger
      than the limit is refused (it would silently cap, hiding the over-limit
      declaration); a zero/negative amount is refused (it is not a
      borrow-to-invest candidate, it is the no-draw baseline the sweep already
      runs implicitly).
    * ``target_account`` must be ``non_reg``. Registered targets (RRSP/TFSA)
      are non-deductible under s.18(11) and are refused loudly until a
      separate slice models them.

    Returns ``[]`` when the household declares none: an empty list is a
    household with no borrow-to-invest question (the golden path), and the
    caller keeps the key out of the internal shape entirely so a
    no-borrow-to-invest household round-trips byte-identically, matching every
    other optional block (DP#24/DP#32). The unwind trigger (#1017 decumulation
    lever) is NOT modelled in this slice.
    """
    borrow_to_invest = list(doc["decisions"].get("borrow_to_invest", []))
    if not borrow_to_invest:
        return []

    # The set of declared kind=heloc liability ids + their limits, for
    # source resolution and the amount<=limit check. The engine supports a
    # single HELOC facility today (_find_liability returns one), but the
    # schema permits more than one, so resolve by id across all of them.
    heloc_limits = {
        liab["id"]: liab["limit"]
        for liab in doc.get("liabilities", [])
        if liab["kind"] == "heloc"
    }
    # The engine models ONE HELOC facility today: `heloc` (found by
    # contract_liabilities.resolve_liability_facilities, scoped to the
    # principal residence then any heloc) is that facility, and
    # `property.margin_available` is ITS limit. The single-facility charge
    # check refuses two helocs on the PRINCIPAL residence, but a SECOND heloc
    # on a non-principal (recreational/rental) property passes it -- so
    # `heloc_limits` can hold more than one entry (D5). A borrow-to-invest
    # source must be the FACILITY heloc (`heloc`): the draw is booked on
    # `new_heloc_balance` (the engine's one drawn margin), at the facility's
    # rate and charge room. A source naming any other heloc (e.g. a
    # recreational-property line) would pass the amount<=limit check against
    # THAT line's limit while the draw actually hits the principal facility --
    # silently aliasing another liability, exactly the DP#32 defect. Refuse
    # unless source == the facility.
    facility_heloc_id = heloc["id"] if heloc else None
    btv_options: List[Dict[str, Any]] = []
    for opt in borrow_to_invest:
        source_id = opt["source"]
        if source_id not in heloc_limits:
            raise ContractAdaptationError(
                f"decisions.borrow_to_invest[id={opt['id']!r}] declares "
                f"source={source_id!r}, but no kind=heloc liability with "
                f"that id is declared (declared heloc ids: "
                f"{sorted(heloc_limits)}). This slice draws against a "
                f"property-secured HELOC only; a margin account or an "
                f"unsecured line_of_credit source is follow-up. Refusing "
                f"loudly rather than silently drawing against nothing or "
                f"silently aliasing another liability (DP#32)."
            )
        if facility_heloc_id is None or source_id != facility_heloc_id:
            raise ContractAdaptationError(
                f"decisions.borrow_to_invest[id={opt['id']!r}] declares "
                f"source={source_id!r}, but the engine's single drawn "
                f"HELOC facility is {facility_heloc_id!r} (whose limit is "
                f"property.margin_available). A draw against any other "
                f"heloc -- e.g. a non-principal-property line -- would pass "
                f"the amount<=limit check against THAT line while the draw "
                f"actually books on the principal facility, silently "
                f"aliasing another liability (D5/DP#32). Refusing loudly; "
                f"multi-facility support is follow-up."
            )
        amount = opt["amount"]
        if amount <= 0:
            raise ContractAdaptationError(
                f"decisions.borrow_to_invest[id={opt['id']!r}] declares "
                f"amount={amount}, which is not a borrow-to-invest draw. "
                f"The no-draw baseline is the implicit amount=0 rung the "
                f"sweep already includes; a declared option must be a real "
                f"draw > 0. Refusing loudly (DP#32)."
            )
        source_limit = heloc_limits[source_id]
        if amount > source_limit:
            raise ContractAdaptationError(
                f"decisions.borrow_to_invest[id={opt['id']!r}] declares "
                f"amount={amount:,.0f} > source {source_id!r}'s limit "
                f"({source_limit:,.0f}). The draw is capped at the HELOC's "
                f"limit at runtime; declaring an over-limit amount would "
                f"silently cap, hiding the over-limit declaration. Refusing "
                f"loudly rather than silently drawing less than declared "
                f"(DP#32)."
            )
        # target_account is enforced by the schema's enum ("non_reg" only)
        # -- a registered target (RRSP/TFSA) is non-deductible under s.18(11)
        # and is refused loudly at schema validation, never silently coerced.
        # No duplicate check here (DP#9: validate_contract enforces the enum
        # before this loop runs).
        btv_entry = {
            "id": opt["id"],
            "label": opt["label"],
            "source": source_id,
            "amount": amount,
            "target_account": opt["target_account"],
        }
        # Issue #1040: hold_draw is OPTIONAL (the schema does not require
        # it and declares no default), so absence and false are the SAME
        # value here -- both mean 'run the existing RRSP-refund paydown
        # sweep' (DP#32: the fallback is for absent input, and the schema
        # does not distinguish absent from false). Emitted only when
        # declared true so a config that omits it round-trips byte-
        # identically (DP#24) -- the pre-#1040 internal shape is unchanged.
        if opt.get("hold_draw"):
            btv_entry["hold_draw"] = True
        btv_options.append(btv_entry)
    return btv_options
