#!/usr/bin/env python3
"""The input contract (issue #602, epic #603, Track C, Phase 2b).

This module is the ONE loading boundary between the dated/owned/entity shaped
input document described by #596-#601 and the engine. It is the SOLE wire
format the engine accepts -- there is no other schema a caller may hand to
``SimulationConfig.from_json`` (Phase 1's ``input_schema.json`` /
``countries/canada/input_schema.json`` example-instance files, and the loose,
unvalidated dict shape they described, are deleted -- DP#9).

``load_and_map()`` is the one function every loading boundary
(``SimulationConfig.from_json`` and every CLI script's ``--input`` flag)
calls to go from an on-disk contract document to the internal shape
``SimulationConfig.from_dict`` accepts. There is exactly one path in,
mandatory, not an opt-in step a caller can bypass.

``to_internal_config()`` is that mapping: single, total, explicit. It is NOT a
second wire format -- nothing outside this adapter and ``SimulationConfig``
itself ever authors the internal shape from scratch as a document on disk; it
exists only as ``SimulationConfig`` / ``ScenarioOverlay`` / ``apply_overlay``'s
in-memory working representation for scenario generation (DP#18/DP#24), a
mechanism entirely internal to the simulation/optimization layers (DP#25).

WHERE THE MAPPING LIVES
-----------------------
This file is the ORCHESTRATOR. It reads as the sequence the document is
consumed in, and every block of real mapping work lives in a module named for
the contract namespace it consumes -- so a reviewer looking for how a leaf
reaches the engine opens exactly one file:

===========================  =============================================
``contract_errors``          the two ways a document is refused
``contract_schema``          compose the two schema files, validate, load
``contract_people``          ``people[]`` -> members, children, admission
``contract_accounts``        ``accounts[]`` -> the household pots + overrides
``contract_liabilities``     ``liabilities[]`` -> mortgage/HELOC/LOC/consumer
``contract_principal``       the principal residence -> ``cfg['property']``
``contract_property``        the other ``properties[]`` -> ``cfg['properties']``
``contract_estate``          ``estate`` + mortality + the PRE family window
``contract_decisions``       ``decisions`` -> the levers the optimizer sweeps
``contract_assumptions``     ``assumptions`` + ``household_budget`` -> beliefs
``contract_transfers``       loans, gifts, installments, grants, cash flows
===========================  =============================================

KNOWN, DECLARED LIMITATION (narrowed by #698 / #643 Step 8, still real): the
internal engine drives its full ADULT compute -- per-adult RRSP/TFSA/FHSA
contribution+growth, spousal RRSP, benefit decumulation, per-adult tax -- for
exactly ONE couple. ``SimState.initial`` seeds only primary+spouse, the
``YearWorkingState`` carries two RRSP/TFSA scalars, and ``simulate_year_pure``'s
signature is primary/spouse marginal rate (the N-slot rule interface Step 5
deferred). This mapping therefore selects ONE couple (the pair named by
``decisions.horizon.person`` and their spouse, or the first ``spouse_of`` pair
found). Steps 2-7 made the account stores (#700), tax loop (#701) and estate
(#705) iterate members, and epic #841 + Step 6 made every DEPENDENT child a
first-class savings/tax subject -- so #698 relaxed the boundary to admit
additional dependent GENERATIONS (of any depth: a grandchild reaches
``_map_child`` exactly like a direct child), and #899 admits an additional
ADULT when they are a pure accumulator across the whole horizon. What is STILL
refused loudly (not silently truncated) is a DECUMULATING additional adult --
a second couple, a benefit-drawing grandparent -- because holding them needs
the N-adult compute rewrite (#706 / Step 9, #901), not just a wider boundary.

What this mapping does NOT reach is no longer a hand-maintained list in this
module (#644, DP#9: one spelling of a rule, not three). That list had drifted
into asserting things that were false -- it still declared the whole ``estate``
namespace unmapped long after #600 (PR #640) wired it for real -- and a list
nothing re-checks is worse than no list, because a reader believes it.

The single, MEASURED answer now lives in
``tests/architecture/test_contract_reachability.py``: it mutates every leaf of
the contract, runs this adapter, and reports what actually moves. It cannot
drift, because a claim that a key is dead fails the build the day the key comes
alive -- and vice versa.
"""

from __future__ import annotations

from typing import Any, Dict

from contract_accounts import (
    _map_account_overrides, map_account_pots, map_management_fee_legs,
)
from contract_assumptions import (
    apply_rate_path_reconciliation, apply_spending_reconciliation,
    map_assumptions, map_emergency_reserve, map_household_budget,
    map_retirement,
)
from contract_decisions import (
    map_borrow_to_invest, map_contribution_strategies, map_declared_objective,
    map_income_scenarios, map_mortgage_decisions, map_resp_action_scenarios,
)
from contract_estate import _family_pre_window, _map_estate, map_insurance_premiums
from contract_liabilities import map_consumer_loans, resolve_liability_facilities
from contract_people import (
    _find_primary_and_spouse, _horizon_end_year, _map_child,
    _map_registered_balances, _people_by_id, admit_people, map_members,
)
from contract_principal import map_property_config
from contract_property import _find_property, _map_owned_properties
from contract_schema import load_contract_json, validate_contract
from contract_transfers import (
    map_cash_flows, map_equity_grants, map_installments, _map_first_home_purchases,
    _map_gifts, _map_private_loans, _map_zev_purchases,
)
from transaction_costs import map_transaction_costs


def to_internal_config(doc: Dict) -> Dict:
    """The single, total, explicit mapping: contract document ->
    ``SimulationConfig``'s internal dict shape (the one
    ``SimulationConfig.from_dict`` accepts, UNCHANGED by this module -- see
    the module docstring for why that is not a second wire format). See the
    module docstring for the documented scope limit. Issue #698 (Step 8 of
    #643) relaxed the couple-only boundary: additional DEPENDENT generations
    (of any depth -- grandchildren) are now admitted through the N-child seam,
    while additional ADULTS a second couple would need are still refused (the
    two-slot compute residual, #706/Step 9). One couple + their children still
    maps byte-identically.

    Requires a document that already passes ``validate_contract`` (called
    here first, so every schema-required key this function indexes directly
    -- rather than defensively ``.get(..., default)``-ing -- is genuinely
    guaranteed present; a caller who skips validation gets a clear
    ContractValidationError here, not a confusing KeyError three functions
    deep)."""
    validate_contract(doc)
    primary_id, spouse_id = _find_primary_and_spouse(doc)
    people = _people_by_id(doc)
    couple = {primary_id, spouse_id} - {None}
    horizon_end = _horizon_end_year(doc, primary_id)
    child_ids, extra_adult_ids = admit_people(doc, people, couple, horizon_end)

    # #647: every rrsp/tfsa/spousal_rrsp/fhsa account's opening balance,
    # attributed to its owner or refused loudly -- computed once, up front,
    # so the refusal happens before any partial mapping is built.
    registered_balances = _map_registered_balances(
        doc, primary_id, spouse_id, child_ids, extra_adult_ids)
    members = map_members(doc, primary_id, spouse_id, extra_adult_ids,
                          registered_balances)
    children = [_map_child(doc, cid, registered_balances) for cid in child_ids]

    as_of = doc["as_of"]
    start_year = int(as_of[:4])
    # Issue #967: the projection span (the number of calendar years the
    # fold simulates) is needed at MAP time to build a mid-horizon mortgage's
    # annual amortization schedule (the schedule must cover the payoff year
    # OR the horizon, whichever comes first, but not build entries past the
    # run). It is derived from the horizon EXACTLY as SimulationConfig.
    # projection_span does (horizon_end - start_year + 1, the inclusive span),
    # so the schedule's length agrees with the fold's length -- the one
    # spelling of the span (DP#9/DP#1). The schedule ALSO stops at the
    # mortgage's own payoff year (balance -> 0), so the cap is a safety
    # bound, not a correctness term; when the horizon does not date against
    # the primary (horizon_end is None) a generous fixed cap is used -- a
    # mortgage's amortization_years (capped by its own payoff) is the real
    # bound, so the cap never truncates a real schedule. A household with no
    # financed property never reads this -- it stays 0.
    if horizon_end is not None:
        projection_years = max(1, horizon_end - start_year + 1)
    else:
        projection_years = 100

    principal = _find_property(doc, "principal")
    mortgage, heloc, credit_facility = resolve_liability_facilities(doc, principal)

    # Issue #969: the family-level PRE designation window (ITA s.40(2)(b)),
    # computed ONCE here so the principal sale and every non-principal sale
    # price their disposition gain against the SAME family denominator the
    # estate path uses (DP#9 -- one spelling of the family window, not each
    # property's own span in isolation). ``None`` is the byte-identical
    # legacy sentinel for a household with no family contest (a single
    # property, or no designation declared anywhere): a sale there falls back
    # to the property's own window, unchanged. The one-per-family-per-year
    # conflict is rejected inside ``_family_pre_window`` (loudly, DP#32) at
    # this ONE contract-loading boundary, so a double-claiming document
    # fails before any sale or the estate prices a gain.
    couple_list = [pid for pid in (primary_id, spouse_id) if pid is not None]
    family_pre_window = _family_pre_window(doc, couple_list, start_year)
    prop_cfg = map_property_config(principal, mortgage, heloc,
                                   credit_facility, primary_id, spouse_id,
                                   family_pre_window)

    consumer_loans = map_consumer_loans(doc)
    refinance_scenarios = map_mortgage_decisions(doc, prop_cfg)

    accounts_cfg, lira_cfg, lsif_cfg, portfolio_cfg = map_account_pots(doc, as_of)
    assumptions = doc["assumptions"]
    assumptions_cfg, resp_account_settings = map_assumptions(doc, start_year)
    accounts_cfg.update(resp_account_settings)

    # Issue #823: per-account expected_return / locked_until overrides,
    # blended into pot-keyed structures the growth + solvency rules read.
    # Empty when no account declares either (golden: global rate, fully
    # liquid -- today's behaviour, DP#32).
    account_overrides = _map_account_overrides(doc)
    accounts_cfg["return_overrides"] = account_overrides["return_overrides"]
    accounts_cfg["locked"] = account_overrides["locked"]
    # Issue #691: per-account MER fees, pot-keyed, subtracted from the gross
    # growth rate. Empty when no account declares a `mer` (golden: fee-free).
    accounts_cfg["mer_drag"] = account_overrides["mer_drag"]
    # Issue #142: each declared non-reg deductible_management_fee_annual is
    # attributed pro rata to its owner(s); the per-person annual totals ride
    # OUT on the member records (family.members round-trips wholesale,
    # DP#24) so both tax folds -- the working-phase prologue and the
    # retirement drawdown base -- can read their own member's fee. Absent
    # fees -> no member carries a key -> byte-identical fold (DP#32).
    mgmt_fees_by_person = account_overrides["mgmt_fees"]
    if mgmt_fees_by_person:
        for _m in members:
            _fee = mgmt_fees_by_person.get(_m["id"])
            if _fee is not None:
                _m["mgmt_fee_non_reg_annual"] = float(_fee)

    horizon = doc["decisions"]["horizon"]  # schema-required
    if horizon["person"] == primary_id:
        assumptions_cfg["horizon_age"] = horizon["until_age"]

    apply_rate_path_reconciliation(assumptions_cfg, assumptions, mortgage, heloc)

    retirement_out = map_retirement(doc)
    income_scenarios = map_income_scenarios(doc, primary_id, spouse_id)
    resp_action_scenarios = map_resp_action_scenarios(doc)
    strategies_cfg, declared_targets = map_contribution_strategies(doc)
    if declared_targets:
        assumptions_cfg["deduct_later_bracket_target"] = declared_targets.pop()

    estate_cfg = _map_estate(doc, primary_id, spouse_id)
    household_budget_out = map_household_budget(doc)
    reserve_out = map_emergency_reserve(doc, spouse_id)
    legacy_cash_flows = map_cash_flows(doc, mortgage, start_year)
    # Issue #139: declared one-time transaction costs / credits join the SAME
    # dated cash-flow channel as the mortgage's origination cash-back (#1070),
    # so every objective that folds the balance sheet (net benefit, solvency,
    # estate) sees them through one engine read (DP#8). Absent block -> no legs
    # -> byte-identical fold (DP#32).
    transaction_cost_legs = map_transaction_costs(doc, start_year)
    if transaction_cost_legs:
        legacy_cash_flows = legacy_cash_flows + transaction_cost_legs
    # Issue #138: each declared life-insurance policy's premium_annual joins
    # the SAME dated cash-flow channel -- one negative leg per in-force year,
    # stopping at the term cliff (a lapsed policy charges nothing; a renewed
    # one keeps its death benefit but its renewal premium is insurer-set and
    # deliberately not priced) -- so every objective sees the true cost of
    # coverage. Absent policies -> no legs -> byte-identical fold (DP#32).
    insurance_premium_legs = map_insurance_premiums(doc, primary_id, start_year)
    if insurance_premium_legs:
        legacy_cash_flows = legacy_cash_flows + insurance_premium_legs
    # Issue #142: each declared non-registered management fee joins the SAME
    # dated cash-flow channel -- one negative leg per projection year (a
    # discretionary mandate charges while the account exists) -- so the fee
    # leaves the household's cash flow instead of being a phantom deduction.
    # Absent fees -> no legs -> byte-identical fold (DP#32).
    management_fee_legs = map_management_fee_legs(doc, start_year)
    if management_fee_legs:
        legacy_cash_flows = legacy_cash_flows + management_fee_legs

    legacy: Dict[str, Any] = {
        "assumptions": assumptions_cfg,
        "estate": estate_cfg,
        "savings": {"rate": assumptions["savings_rate"]},
        "property": prop_cfg,
        "family": {"members": members, "children": children,
                   "private_loans": _map_private_loans(doc),
                   # Epic #841 bite 3: parent->child gifts funding a child's
                   # registered room. Donor must be a declared member, recipient
                   # a declared child (DP#32 loud refusal on a typo).
                   "gifts": _map_gifts(
                       doc,
                       {m["id"] for m in members},
                       {c["id"] for c in children}),
                   # Issues #704/#931: a first-time home purchase -> FHSA
                   # tax-free withdrawal + HBP, for a declared child (own
                   # accounts) OR adult (household FHSA / own RRSP). Buyer must be
                   # a declared member (DP#32 loud refusal on a typo).
                   "first_home_purchases": _map_first_home_purchases(
                       doc, {c["id"] for c in children},
                       {m["id"] for m in members})},
        "accounts": accounts_cfg,
        # A dated zero-emission vehicle acquisition. Household-level, not
        # member-level: the incentive is paid on the vehicle, not to a person.
        "zev_purchases": _map_zev_purchases(doc),
        "tax": {"country": doc["jurisdiction"]["country"], "province": doc["jurisdiction"]["province"]},
        "cash_flows": legacy_cash_flows,
    }

    declared_objective = map_declared_objective(doc)
    if declared_objective is not None:
        legacy["objective"] = declared_objective

    if portfolio_cfg:
        legacy["portfolio"] = portfolio_cfg
    if retirement_out:
        legacy["retirement"] = retirement_out
    if "return_model" in assumptions:
        legacy["return_model"] = assumptions["return_model"]
    if lira_cfg:
        legacy["lira"] = lira_cfg
    if lsif_cfg:
        legacy["lsif"] = lsif_cfg
    if household_budget_out:
        legacy["household_budget"] = household_budget_out
    if reserve_out:
        # Issue #688: SimulationConfig.from_dict reads the reserve policy off
        # `assumptions.emergency_reserve` (see _reserve_cfg there), and
        # apply_overlay writes the swept target back to the same key -- one
        # spelling, so the sweep cannot land on a key the engine never reads
        # (#591/DP#18).
        assumptions_cfg["emergency_reserve"] = reserve_out
    if income_scenarios or refinance_scenarios or resp_action_scenarios:
        legacy["scenarios"] = {}
        if income_scenarios:
            legacy["scenarios"]["income"] = income_scenarios
        if refinance_scenarios:
            # Phase 2c fix (see contract_decisions.map_mortgage_decisions): a
            # refinance option is a CASH-OUT decision, and this is the block
            # whose consumer actually reads `cash_out`.
            legacy["scenarios"]["refinance"] = refinance_scenarios
        if resp_action_scenarios:
            legacy["scenarios"]["resp_action"] = resp_action_scenarios

    # Issue #713: the authored strategies reach the optimizer's search space
    # (optimize.py hands this to discover_strategies(custom_strategies=...)).
    # Mapped only when the household actually declared some: an empty
    # contribution_strategy[] means "no opinion", and must leave the optimizer
    # on its discovered defaults rather than hand it an empty search space
    # (DP#13 -- a default is a fallback for ABSENT input, never a way to
    # overrule a supplied one).
    if strategies_cfg:
        legacy["strategies"] = strategies_cfg

    # Issue #763: closed-end consumer loans (car_loan/student_loan/
    # personal_loan) reach the engine's debt-service / solvency / runway path
    # as a first-class `consumer_loans` list (SimulationConfig.consumer_loans
    # -> simulation_rules.apply_consumer_loans). Mapped only when the
    # household actually declares some: an empty list would be a no-op, but
    # emitting it would still pass _validate_internal_shape (the key is
    # allowed) -- kept conditional so the internal shape round-trips to
    # "absent" for a household with no consumer debt, matching every other
    # optional block above (DP#24/DP#32).
    if consumer_loans:
        legacy["consumer_loans"] = consumer_loans

    # Issue #936: deposit products (a plain HISA, a term/GIC, a promotional
    # teaser -- one generic mechanism) reach the optimizer's take/leave sweep as
    # a first-class `deposit_products` list (scenario_discovery
    # ._discover_deposit_products -> simulate.enumerate_overlays ->
    # ScenarioOverlay.deposit_product -> apply_overlay -> SimulationConfig
    # .deposit_product -> SimState.initial carve + simulation_rules
    # .apply_deposit_product_growth). Each entry passes through unchanged -- it is
    # already the {id, label, account_kind, fund_amount, funding_source,
    # rate_schedule, rate_eligible_cap (optional), tax_character} shape the sweep
    # reads.
    # Mapped only when the household actually declares some: an empty list is a
    # household with no such product (the golden path), and emitting it would move
    # the internal shape away from "absent" -- kept conditional so a
    # no-product household round-trips byte-identically, matching every other
    # optional block above (DP#24/DP#32). Absence => no product swept, the golden
    # trajectory is byte-identical (the companion staggered/sequence-of-returns
    # evaluation is issue #937).
    deposit_products = list(doc["decisions"].get("deposit_products", []))
    if deposit_products:
        legacy["deposit_products"] = deposit_products

    # Issue #1036: decisions.borrow_to_invest -- the one-shot, swept,
    # objective-ranked leverage decision (draw against the declared HELOC at
    # year 0, invest in non-reg, deduct the interest under ITA s.20(1)(c)).
    # Mapped (and its DP#32 boundary refusals raised) by
    # contract_decisions.map_borrow_to_invest, which needs the resolved HELOC
    # facility to reject a source naming any other heloc. Emitted only when
    # the household declares some: an empty list is a household with no
    # borrow-to-invest question (the golden path), so a no-borrow-to-invest
    # household round-trips byte-identically (DP#24/DP#32).
    btv_options = map_borrow_to_invest(doc, heloc)
    if btv_options:
        legacy["borrow_to_invest_options"] = btv_options

    # Issue #692 (epic #690 bite 1): the couple's NON-principal properties reach
    # the annual balance sheet as a first-class `properties` list
    # (SimulationConfig.properties -> SimState.property_equities -> total_assets).
    # Mapped only when the couple actually owns one: an empty list is a
    # household with no such property (the golden path), and emitting it would
    # move the internal shape away from "absent" -- kept conditional so a
    # no-non-principal-property household round-trips byte-identically, matching
    # every other optional block above (DP#24/DP#32).
    owned_properties = _map_owned_properties(doc, primary_id, spouse_id,
                                             projection_years=projection_years,
                                             family_pre_window=family_pre_window)
    if owned_properties:
        legacy["properties"] = owned_properties

    installment_plans = map_installments(doc)
    if installment_plans:
        legacy["installments"] = installment_plans

    equity_grants = map_equity_grants(doc)
    if equity_grants:
        legacy["equity_grants"] = equity_grants

    presets = doc["sensitivity"]["presets"]  # both schema-required (may be {})
    if presets:
        legacy["sensitivity_overlay_presets"] = presets

    apply_spending_reconciliation(assumptions_cfg, doc, household_budget_out,
                                  retirement_out)

    return legacy


def load_and_map(path: str) -> Dict:
    """The ONE loading boundary (epic #603 Phase 2b): read a contract
    document off disk, validate it, and map it to SimulationConfig's
    internal dict shape. Every entry point that used to ``json.load()`` an
    ``--input``/``input.json`` path directly and hand the raw dict onward
    (``SimulationConfig.from_json``, and every CLI script's ``--input``
    flag: ``simulate.py``, ``optimize.py``, ``retirement_analysis.py``,
    ``countries/canada/resp_rules.py``) now calls this instead -- there is
    exactly one path from an on-disk document to a config the engine can
    run, mandatory, never a bypassable adapter step (DP#32)."""
    doc = load_contract_json(path)
    validate_contract(doc)
    return to_internal_config(doc)
