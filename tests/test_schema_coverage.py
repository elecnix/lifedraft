#!/usr/bin/env python3
"""Schema coverage guard (issue #582, epic #603; DP#14, DP#32).

Epic #603 Track C Phase 2b rewrite: this guard used to walk the LEGACY
example-instance files (``input_schema.json`` / ``countries/canada/
input_schema.json``) -- both deleted now (DP#9), along with
``adapt_contract_to_legacy``'s status as an optional bridge between two live
shapes. There is one shape now: the input contract (``schema/input_schema.
json`` + ``schema/countries/canada/input_schema.json``, composed by
``input_contract.compose_schema()``), and this guard walks the contract's
own populated example (``schema/example.json``, Phase 1's 4-generation
household) as the representative instance -- the same technique the old
guard used, pointed at the real thing instead of a legacy stand-in.

GUARD 1 — every schema leaf is either consumed or explicitly allowlisted.
    ``DEAD_ALLOWLIST`` is the itemised migration checklist: one entry per
    leaf that is parsed but never reaches a decision, each naming the issue
    that explains why. ``CONSUMED`` is the complement: for every leaf NOT in
    the allowlist, a ``(file, keyword)`` citation naming the *one*
    production line where the leaf's value reaches a real decision.

    A leaf's journey from the contract document to a decision has TWO hops:
    ``input_contract.to_internal_config()`` maps the validated document onto
    ``SimulationConfig``'s internal dict shape (unchanged by this phase --
    see ``simulation_config.py``'s docstrings), and then the EXISTING,
    unmodified downstream code (``simulation_state.py``, ``simulation_
    rules.py``, ``optimize.py``, ``scenario_discovery.py``, ...) reads that
    internal shape exactly as it always has. A citation may legitimately
    point at ``input_contract.py`` itself when the "real decision" IS the
    mapping/selection logic there (e.g. choosing which person is primary
    vs. spouse from ``relationships[].type``, or computing an age from a
    date) -- that is doing real work, not merely copying a key, and
    ``input_contract.py`` is deliberately NOT in the excluded-loader set
    below (unlike ``simulation_config.py``). For a leaf whose value flows
    through unchanged into an internal-shape field some pre-existing,
    already-verified downstream consumer reads, the citation points at that
    deeper, more specific consumer instead (more useful, matches this
    guard's original philosophy).

    Both registries are mechanically re-verified on every run: a leaf that
    is neither allowlisted nor cited fails loudly, an allowlist entry for a
    leaf that no longer exists fails loudly, and a CONSUMED citation whose
    keyword no longer appears in its file fails loudly.

GUARD 2 — unknown keys are rejected.
    Superseded by real, total ``additionalProperties: false`` JSON Schema
    validation at the ONE loading boundary (``input_contract.
    validate_contract``, called from ``SimulationConfig.from_json`` /
    every CLI script's ``--input`` flag via ``input_contract.load_and_map``)
    -- see ``tests/test_input_contract.py``'s ``RejectionTest`` class, which
    is the real, current version of this guard. ``SimulationConfig.
    from_dict``'s OWN, narrower unknown-key guard (``_validate_internal_
    shape``, scoped to the internal dict shape ``from_dict`` still accepts
    from ``apply_overlay``/``ScenarioOverlay``/the optimizer's scenario
    generation -- see that module's docstrings) is covered by
    ``test_unknown_key_is_rejected`` below, carried over unchanged.

## Findings surfaced while rebuilding this guard for the new contract (Phase 2b)

Verifying every new-schema leaf against the code (not assuming Phase 1's
mapping was complete) surfaced real gaps Phase 1 could not have known about
without this guard existing:

- ``portfolio.accounts.non_reg.{balance,cost_basis}`` -- the pair Track C
  Phase 2a left un-deleted on purpose (the only place a non-registered
  opening balance/ACB could be declared, but never read back) -- is now
  WIRED: ``SimState.initial()`` reads the mapped opening non-reg balance/ACB
  for real (``simulation_state.py``). This was the #599 follow-up Phase 2a
  flagged and deferred.
- ``assumptions.resp.{study_start_age,study_duration_years,used_for_
  education}`` (issue #578's winddown fields) had NO home anywhere in the
  Phase 1 contract schema at all -- worse than dead, genuinely
  unrepresentable. Added to the Canada overlay's ``assumptions.resp`` $def
  and wired into ``to_internal_config`` in this PR.
- Root-level ``currency``/``dollars``/``real_base_year`` (the document's own
  units, #597) had no path into the internal shape either, so
  ``model_fidelity.describe_units`` (#585 -- declares nominal-vs-real on
  every output surface) silently lost its input for every contract-sourced
  run. Mapped for real in this PR.
- ``adapt_contract_to_legacy`` (Phase 1) was writing THREE duplicate
  declarations into dead corners of the internal shape that Track C Phase 2a
  had already confirmed dead and deleted from the legacy schema:
  ``legacy["heloc"]`` (the whole block -- HELOCConfig/heloc_data were
  deleted in Phase 2a; nothing reads ``cfg['heloc']`` anywhere, confirmed by
  grep), ``property.current_payment_monthly``/``.renewal_date``/
  ``.contract_start_date`` (confirmed dead in Phase 2a, still dead), and
  ``lira.source_pension_plan``/``.transfer_date`` (same). Deleted from
  ``to_internal_config`` in this PR rather than carried forward as
  duplicate declarations reaching nothing (DP#9/DP#18/DP#32) -- see that
  module's inline comments for the full evidence trail.
- ``decisions.mortgage.refinance_options[]``/``renewal_options[]`` -- a
  PRE-EXISTING Phase 1 shape mismatch, found (not introduced) by this
  guard: ``scenario_discovery.py``'s consumer reads ``option.get('name')``/
  ``.get('rate')``/``.get('type')``/``.get('term_years')``, but a
  ``refinance_option`` has ``id``/``label``/``cash_out``/``ltv`` -- none of
  which match, so a refinance option's real content (``cash_out``, ``ltv``)
  is silently ignored and every refinance option renders as "Refinance:
  Unknown option" at hardcoded defaults. A ``renewal_option`` DOES share
  ``rate``/``type``/``term_years`` field names, so those three genuinely
  flow through; ``id``/``label`` do not (the consumer looks for a ``name``
  key neither shape has). Documented honestly below as DEAD_ALLOWLIST
  entries (issue-worthy follow-up, not fixed in this PR -- fixing
  ``scenario_discovery.py``'s consumer shape is separate, real work outside
  Phase 2b's ingestion-boundary scope).
- ``people[].study_periods[]`` and ``people[].room.resp`` are parsed by
  ``to_internal_config`` (the per-child study window; the Canada overlay's
  documented "lifetime beneficiary entitlement" RESP room pool) but reach no
  downstream reader at all -- confirmed by grep, not merely assumed.

## Corrections carried over from the legacy guard's own history (context only)

The legacy guard's history (module_registry.check_auto_includes never having
a production caller; the #574/#575/#576 fixes wiring up rrsp/tfsa balance
and non_reg yield composition; family_integration.py's deletion; etc.) is
preserved in this file's git history (see the epic #603 Track C Phase 2a PR)
-- not repeated here since the legacy schema files and the code paths those
findings were about are now deleted.
"""

import json
import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSAL_SCHEMA_PATH = REPO_ROOT / "schema" / "input_schema.json"
CANADA_SCHEMA_PATH = REPO_ROOT / "schema" / "countries" / "canada" / "input_schema.json"
EXAMPLE_PATH = REPO_ROOT / "schema" / "example.json"


# ── Leaf walker ──────────────────────────────────────────────────────────
#
# Walks a schema *instance* down to its scalar/null leaves. A dotted path
# with a trailing "[]" segment marks "one field of an item in this array".
# UNLIKE the legacy guard's walker, this one unions leaves across EVERY
# element of an array, not just the first: the contract's arrays are
# genuinely polymorphic (an `accounts[]` entry's shape depends on `kind` --
# an rrsp and an lsif account share almost none of their kind-specific
# fields), so walking only element [0] would make entire branches (fhsa/
# lira/lsif/resp sub-objects, benefit types, relationship types) invisible
# to this guard depending on which kind happened to sort first.

def _iter_leaves(node, path, out):
    if isinstance(node, dict):
        visible_keys = [k for k in node.keys() if not k.startswith("$")]
        if not visible_keys:
            # Empty object -- still a real leaf: the object's mere
            # presence/emptiness is meaningful.
            out.append(path)
            return
        for key, value in node.items():
            if key.startswith("$"):
                continue
            _iter_leaves(value, f"{path}.{key}" if path else key, out)
    elif isinstance(node, list):
        if not node:
            out.append(path)
        elif isinstance(node[0], dict):
            # Issue #647: the top-level `accounts[]` array is polymorphic on
            # its `kind` discriminator (twelve values -- rrsp, spousal_rrsp,
            # tfsa, fhsa, rrif, lif, lira, resp, non_reg, dcpp, dbpp, lsif),
            # and different kinds reach the engine through COMPLETELY
            # different code paths (or none at all). A bare `accounts[].
            # balance.amount` leaf unions every kind's balance into ONE leaf
            # -- so a single citation proving `kind=rrsp` is consumed
            # silently certified the identical leaf as consumed for the
            # other eleven kinds too, including spousal_rrsp/fhsa, which
            # were dropping $72,000 with this guard fully green (#647).
            # Tag each element's leaves with its own kind instead
            # (`accounts[kind=rrsp].balance.amount`,
            # `accounts[kind=fhsa].balance.amount`, ...) so CONSUMED/
            # DEAD_ALLOWLIST must classify each (leaf, kind) pair on its own
            # evidence. Originally scoped to `accounts` only (a narrower,
            # deliberate fix for the exact blindness #647 found, not a
            # rewrite of every polymorphic array in the schema) -- extended
            # to `liabilities` by issue #654, which found the identical
            # blindness there: a single kind=mortgage citation for the
            # bare `liabilities[].rate` leaf was silently vouching for
            # kind=heloc's rate too, which reached no consumer at all
            # (the HELOC interest path was built from property.
            # mortgage_rate instead, #654's actual bug). `properties[]` is
            # still NOT kind-split -- no comparable per-kind blindness has
            # been found there yet; extend it the same way if one is.
            if path in ("accounts", "liabilities") and all("kind" in item for item in node):
                for item in node:
                    _iter_leaves(item, f"{path}[kind={item['kind']}]", out)
            else:
                for item in node:
                    _iter_leaves(item, path + "[]", out)
        else:
            out.append(path)
    else:
        out.append(path)


def schema_leaves(instance_path: Path):
    """Every leaf path in a schema instance file, as a sorted list of str."""
    data = json.loads(instance_path.read_text())
    out = []
    for key, value in data.items():
        if key.startswith("$"):
            continue
        _iter_leaves(value, key, out)
    return sorted(set(out))


ALL_LEAVES = set(schema_leaves(EXAMPLE_PATH))

# The example's product catalog drives the per-product-name leaves below
# (assumptions.products.<name>.<field>) -- read from the same instance file
# rather than hardcoded, so a product added/renamed in schema/example.json
# doesn't silently desync this guard's product-name literals from it.
_EXAMPLE_PRODUCT_NAMES = sorted(json.loads(EXAMPLE_PATH.read_text())["assumptions"]["products"].keys())


# ── Guard 1a: the migration checklist (dead / not-yet-consumed leaves) ──

DEAD_ALLOWLIST = {
    # ── root-level ──
    "schema_version": ("#582", "validated structurally by the schema's own `const` constraint "
        "(jsonschema.Draft202012Validator rejects a mismatched value at validate_contract) -- "
        "not read by any application code beyond that generic check."),
    "jurisdiction.country": ("#603", "maps to legacy tax.country -> SimulationConfig.country, which "
        "reaches no downstream reader -- jurisdiction module selection is done by countries/canada "
        "PACKAGE presence (DP#16), not by this field. A pre-existing gap in the legacy engine (this "
        "field was already effectively unread before epic #603); now visible because the contract "
        "makes jurisdiction.country a real required leaf instead of an undocumented optional key."),

    # ── people[] ──
    "people[].legal_name": ("DP#15", "always null in every fixture/example by design (no personal "
        "data in the repo) -- not read downstream either; a real name has nowhere to go once entered."),
    # #644: these two were in CONSUMED until the reachability detector
    # (tests/architecture/test_contract_reachability.py) measured them as never
    # reaching the internal config at all. Both citations "passed" only because
    # the cited KEYWORD still appeared in the file -- which is exactly the
    # failure mode that epic makes impossible now.
    "people[].relationships[].from": ("#644", "was cited to `for r in people[primary_id].get("
        "\"relationships\", [])` -- a line that iterates the relationship LIST and then reads only "
        "r[\"type\"] and r[\"person\"]. The date a relationship STARTED is parsed and reaches no "
        "decision: the engine has no rule keyed on when a couple married."),
    "people[].relationships[].to": ("#644", "see people[].relationships[].from above -- same false "
        "citation, same non-consumption. (A relationship's END date would matter for a separation "
        "rule; there isn't one.)"),
    "people[].death_date": ("#600", "no legacy consumer -- Phase 2c's estate/mortality wiring."),
    "people[].residency.province": ("#600", "legacy has one household-wide tax.province, not "
        "per-person -- Phase 2c/#598 follow-up."),
    "people[].residency.since": ("#600", "same as people[].residency.province."),
    "people[].incomes[].employer_rrsp_match": ("#595", "#595E's single surviving spelling of the "
        "employer-match fact, but MemberRetirementData.employer_match() (its only would-be consumer) "
        "itself has zero production callers -- nothing to map into yet."),
    "people[].incomes[].employer_rrsp_match.into_account": ("#595", "see employer_rrsp_match above."),
    "people[].incomes[].employer_rrsp_match.max": ("#595", "see employer_rrsp_match above."),
    "people[].incomes[].employer_rrsp_match.pct": ("#595", "see employer_rrsp_match above."),
    # Issue #767: declared employment-contract context that the engine does
    # not (yet) drive a financial decision from. scope/geography are the
    # non-compete's contractual scope -- required by the schema so a partial
    # declaration (months with no scope) FAILS at validation rather than
    # silently narrowing what re-employment is barred (DP#32), but the engine
    # does not model re-employment *outside* the specialty at a lower salary
    # yet (#767 future work). probation_end flags the highest-risk window
    # for a 'now' shock but does not yet alter any probability/cash-flow
    # path. All three are parsed and survive validation; none reaches a
    # decision today -- cited here so the guard sees them honestly rather
    # than as silent dead leaves.
    "people[].incomes[].employment.non_compete.scope": ("#767", "required by schema so a "
        "partial non-compete declaration fails loudly; not yet consumed -- the "
        "engine does not model re-employment outside the specialty at a lower "
        "salary."),
    "people[].incomes[].employment.non_compete.geography": ("#767", "same as .scope -- "
        "contract context required for the partial-declaration guard, not yet "
        "a decision input."),
    "people[].incomes[].employment.probation_end": ("#767", "declares the highest-risk "
        "termination window; not yet wired into any probability/cash-flow path "
        "(#767 flags probation but does not enforce on it)."),
    "people[].benefits.cpp.as_of": ("#603", "to_internal_config maps only start_date/monthly_amount "
        "into member['cpp_start_age']/['cpp_monthly_estimated'] -- the estimate's own as_of date "
        "(when Service Canada issued it) is parsed but reaches no decision."),
    "people[].benefits.employer_pension.start_date": ("#603", "to_internal_config maps only "
        "annual_amount into member['pension_income_annual'] -- the pension's start_date is parsed "
        "but reaches no decision (the engine has no per-member pension-start-date rule yet)."),
    "people[].benefits.employer_pension.indexed": ("#603", "parsed, never read -- the engine has no "
        "pension-indexation rule."),
    "people[].room.fhsa": ("#603", "person_room.fhsa is parsed by the room loop the same as rrsp/tfsa "
        "would be, but FHSA opening room comes from the LEGACY member field fhsa_room_accumulated "
        "which to_internal_config does NOT populate from this contract field -- see FHSA account "
        "balance's identical gap below; wiring FHSA for real is a #599 follow-up, not this PR's job."),
    "people[].room.resp": ("#603", "RESP contribution room is a lifetime BENEFICIARY entitlement per "
        "the Canada overlay's own $def description, but _map_member/_map_child's room loop only "
        "reads rrsp/tfsa/fhsa -- accounts.resp_annual_room_per_child (a flat per-child figure, a "
        "DIFFERENT concept) is what scenario_discovery.py actually reads for RESP room."),
    "people[].room.resp.as_of": ("#603", "see people[].room.resp above."),
    "people[].room.resp.contribution_room": ("#603", "see people[].room.resp above."),
    "people[].room.rrsp.as_of": ("#603", "the room grant's own as_of date is parsed (person_room.rrsp/"
        ".tfsa/.fhsa) but only .contribution_room is read (simulation_state.py) -- the engine has no "
        "rule that depends on WHEN the room figure was current, only its value."),
    "people[].room.tfsa.as_of": ("#603", "see people[].room.rrsp.as_of above."),
    # people[].study_periods[] -- the DATES are WIRED by #714 (see CONSUMED).
    # This bare leaf only exists for a child whose study_periods list is EMPTY
    # (the walker emits an empty list as a leaf in its own right); an empty list
    # declares no window, so the RESP winddown correctly falls back to the
    # age-derived one (DP#13) and there is nothing here for a consumer to read.
    "people[].study_periods": ("#714", "only ever a leaf when the list is EMPTY -- an empty "
        "study_periods declares no window at all, so resp_study_window_for_child falls back to the "
        "age-derived window. The populated case IS consumed (see CONSUMED, start_date/end_date)."),
    "people[].study_periods[].institution": ("#714", "the WINDOW is now consumed (start_date/"
        "end_date -> resp_study_window_for_child), but which school the beneficiary attends reaches "
        "no decision: this engine's RESP has no per-institution rule (no tuition schedule, no "
        "in-province/out-of-province EAP distinction). Descriptive, honestly unread."),
    "people[].study_periods[].program": ("#714", "see people[].study_periods[].institution above -- "
        "same non-consumption. A program name changes no dollar in this model."),

    # ── accounts[] -- kind-blind entries deliberately NOT here anymore (#647).
    # See the "accounts[kind=...]" generated block below CONSUMED, which
    # classifies every (leaf, kind) pair on its own evidence -- a bare
    # "accounts[].X" key would silently re-certify all twelve kinds again,
    # exactly the blindness #647 found. fhsa.*/lira.*/resp.*/lsif.* leaves
    # stay hand-written immediately below (only one or two kinds ever
    # produce them, so a loop buys nothing); balance.as_of/beneficiary/acb/
    # successor_holder/holdings[]/id/owner/kind are generated per-kind.

    "accounts[kind=fhsa].fhsa.opened_date": ("#599", "FHSA accounts are parsed and (#647) the "
        "balance/room DO now reach the engine (member['fhsa_balance']/'fhsa_room_accumulated' via "
        "_map_registered_balances, consumed by SimState.initial()) -- but opened_date specifically "
        "still reaches no decision: the engine has no HBP-15-year-window rule keyed off when the "
        "FHSA was opened."),
    "accounts[kind=fhsa].fhsa.first_time_buyer_since": ("#599", "see accounts[kind=fhsa].fhsa."
        "opened_date above -- same finding, same reason."),
    "accounts[kind=lira].lira.source_pension_plan": ("#603", "confirmed dead and deleted from the "
        "legacy schema in Track C Phase 2a; to_internal_config no longer maps it either (see the "
        "comment on lira_cfg construction)."),
    "accounts[kind=lif].lira.source_pension_plan": ("#603", "see accounts[kind=lira].lira."
        "source_pension_plan above -- kind=lif accounts share the same `lira` sub-object and the "
        "same (non-)consumer."),
    "accounts[kind=lira].lira.transfer_date": ("#603", "see accounts[kind=lira].lira."
        "source_pension_plan above."),
    "accounts[kind=lif].lira.transfer_date": ("#603", "see accounts[kind=lira].lira."
        "source_pension_plan above."),
    "accounts[kind=resp].resp.subscribers": ("#603", "parsed as part of the schema-required `resp` "
        "sub-object, but to_internal_config's resp_composition aggregation (#647) reads only "
        "contributions_total/cesg_received/qesi_received -- the subscriber identities never reach a "
        "decision (the engine's RESP is a per-child balance list, not attributed to a subscriber)."),
    "accounts[kind=resp].resp.beneficiaries": ("#603", "see accounts[kind=resp].resp.subscribers "
        "above -- same non-consumption, same reason."),
    "accounts[kind=resp].resp.clb_received": ("#603", "NOT folded into resp_composition's "
        "contributions/cesg/qesi buckets (#647's aggregation sums only those three) -- a real gap: "
        "CLB dollars land in resp_current_balance but get misclassified as investment_earnings when "
        "the RESP later winds down (issue #578's EAP/PSE split), instead of taxed as a grant like "
        "CESG/QESI. Narrower than #647's balance-drop (the dollar amount is still counted, only its "
        "EAP-tax category is wrong) -- flagged here, not fixed in this PR; worth its own issue."),
    "accounts[kind=resp].owner.joint[].pct": ("#644", "was cited to _owner_shares' `return "
        "{j[\"person\"]: j[\"pct\"] ...}` -- one generic helper's keyword vouching for every kind "
        "that has a joint owner at once (#647's kind-blindness, surviving inside CONSUMED). "
        "_weighted_rolled_fraction/_ownership_share are only ever called with _REGISTERED_KINDS and "
        "non_reg; RESP is in neither, and the RESP itself is aggregated by BALANCE, never by owner "
        "share. So a RESP's joint split is parsed and reaches no decision -- measured, not assumed "
        "(tests/architecture/test_contract_reachability.py)."),
    "accounts[kind=spousal_rrsp].contributors": ("#593", "no legacy consumer -- spousal RRSP "
        "attribution (who CONTRIBUTED, as opposed to who OWNS/receives the annuity, which #647 does "
        "map) is engine-internal, not config-driven."),

    # ── liabilities[] -- kind-blind entries deliberately NOT here (#654,
    # mirroring #647's accounts[] fix). See the "liabilities[kind=...]"
    # generated block after CONSUMED below, which classifies every
    # (leaf, kind) pair on its own evidence -- a bare "liabilities[].X"
    # key silently re-certified all three example kinds (mortgage/heloc/
    # car_loan) from one citation, which is exactly how kind=heloc's
    # `.rate` hid behind kind=mortgage's for as long as it did.

    # ── properties[] ──
    "properties[].value.as_of": ("#597", "same reasoning as accounts[].balance.as_of."),
    # NOTE (#695, epic #690 bite 4): the designated-year RANGES used to be dead
    # here -- only the designation's PRESENCE was read. They are now CONSUMED
    # below: input_contract._map_pre_property_gains reads each period's from/to to
    # compute the per-property taxable fraction (ITA s.40(2)(b)). The SHIPPED
    # example's couple owns a single property, so the ranges cannot move a dollar
    # THERE (a fixture limit, recorded in test_contract_reachability.py's
    # NOT_EXERCISED_BY_EXAMPLE) -- but the consumer is real, so they are cited,
    # not allowlisted.

    # ── estate.* (Phase 2c's whole job -- epic #603) ──
    # ── estate.life_insurance[]: the fields Phase 2c does NOT read. The death benefit reaches the
    # estate as one tax-free pot (ITA s.148(1)), so only `insured` (is it OUR estate?),
    # `face_amount` (how much) and `term_end_date` (has the policy lapsed by the horizon?) move a
    # dollar. The rest describe the POLICY, not its effect on this estate.
    "estate.life_insurance[].id": ("#600", "read only to name the policy in the log line that "
        "reports a lapsed term policy being excluded -- it identifies, it does not decide."),
    "estate.life_insurance[].owner": ("#600", "the POLICY's owner (who pays/controls it) is not who "
        "the benefit is paid on -- that is `insured`, which IS consumed. Owner never reaches a "
        "decision; it would matter for a policy owned by a corporation or a trust, which this "
        "engine does not model."),
    "estate.life_insurance[].beneficiary": ("#600", "the benefit joins the estate as one pot; this "
        "engine does not distribute the estate to individual heirs (see accounts[].beneficiary)."),
    "estate.life_insurance[].kind": ("#600", "term-vs-permanent is DERIVED from term_end_date being "
        "null, which is what the lapse check actually reads -- `kind` is a redundant second "
        "spelling of the same fact (#595) and is never branched on."),
    "estate.life_insurance[].premium_annual": ("#600", "premiums are a lifetime cash outflow; the "
        "engine does not deduct them from the projection (a real gap -- a $3,400/yr permanent "
        "premium over 50 years is real money that never leaves the household's balance sheet)."),
    "estate.life_insurance[].as_of": ("#600", "the face amount's statement date; only the amount is "
        "read (same reasoning as accounts[].balance.as_of)."),
    "decisions.estate_elections[].id": ("#600", "no legacy consumer -- Phase 2c."),
    "decisions.estate_elections[].label": ("#600", "see decisions.estate_elections[].id above."),
    "decisions.estate_elections[].spousal_rollover": ("#600", "see decisions.estate_elections[].id above."),

    # ── decisions.contribution_strategy[] -- WIRED by #713 (see CONSUMED).
    # The block's six allocation leaves all reach a decision now. The four
    # registered-account percentages were spent by #713 (`s.<x>_pct * savings`);
    # tfsa_pct and non_reg_pct were the two #713 left unfixed because fixing them
    # moves the golden trajectory -- that is #751's own PR (see CONSUMED for the
    # citations into StrategyEngine.allocate).

    # ── decisions.retirement_age[] sweep beyond the first candidate ──
    "decisions.retirement_age[].candidate_ages": ("#603", "to_internal_config maps only "
        "candidate_ages[0] into member['retirement_age'] (a scalar per member) -- the legacy engine "
        "has no sweep-list retirement-age field; the FULL list is a Phase 2c/optimizer-integration "
        "concern, not this PR's job. (The first candidate DOES reach a decision -- see CONSUMED.)"),

    # ── decisions.mortgage.* -- the Phase 1 shape mismatch this guard SURFACED is now FIXED in
    # Phase 2c (see input_contract.py's decisions.mortgage comment): a refinance_option is a
    # CASH-OUT decision and now maps to scenarios.refinance[] (whose consumer reads cash_out); a
    # renewal_option is a RATE option and now maps to property.renewal_options with its `label`
    # renamed to the `name` key the consumer actually reads. What remains dead:
    "decisions.mortgage.renewal_options[].id": ("#603", "the mapper carries a renewal option's "
        "label/rate/type/term_years into property.renewal_options, but NOT its id: the internal "
        "consumer (optimize.py's discover_rate_anchors) synthesizes its own anchor key by slugifying "
        "the name, so a stated id has nowhere to land. Harmless, but genuinely unread -- said "
        "plainly rather than cited to a line that does not read it."),
    "decisions.mortgage.refinance_options[].ltv": ("#601", "the contract states the target LTV "
        "explicitly, but scenario_discovery._convert_refinance_scenarios DERIVES ltv from cash_out "
        "and house_value instead (`ltv = (mortgage_balance + cash_out) / house_value`) -- so a "
        "stated ltv that disagrees with its own cash_out would be silently ignored. Two spellings "
        "of one fact (#595); the derived one wins. Left as-is rather than pick a winner in a PR "
        "already this size."),
    "decisions.mortgage.structure_options[].revolving_rate_type": ("#654", "mapped alongside "
        "revolving_rate to property.heloc_rate_type for round-trip completeness (DP#24), same as "
        "liabilities[kind=heloc].rate_type above -- but HELOCPath.get_heloc_rate returns fixed_rate "
        "outright once a HELOC rate is declared, for every year, regardless of rate_type; there is "
        "no year-over-year HELOC rate PATH in this engine yet for 'variable' vs 'fixed' to switch "
        "on. Genuinely parsed (apply_structure_overlay sets it), not yet consumed -- same finding, "
        "not a new one, for the structure-options path (#687)."),

    # ── decisions.resp_action[] ──
    "decisions.resp_action[].label": ("#603", "scenario_discovery.py reads only item['id'] for each "
        "resp_action entry -- label is parsed, never read."),
    "decisions.resp_action[].cash_out": ("#603", "to_internal_config's resp_action_scenarios mapping "
        "drops cash_out entirely (only id/label survive the mapping) -- never reaches the internal "
        "shape at all, let alone a decision."),

    # ── assumptions.emergency_reserve (issue #688) ──
    # The other four leaves of this block are CONSUMED (see below). This one is
    # declared-but-not-yet-read, and is listed here rather than quietly cited
    # against a line that merely PARSES it: the replenishment rule (how a
    # drawn-down reserve gets refilled out of surplus cash, and at what
    # priority against other uses) is real follow-up work, not something #688
    # shipped. Pretending otherwise is exactly the "parsed, mapped, then never
    # passed" shape AGENTS.md lists as a trap this codebase has actually fallen
    # into.
    "assumptions.emergency_reserve.replenish_priority": ("#688", "declared so the reserve "
        "policy is expressible in full, but the replenishment RULE is not implemented yet -- "
        "the engine draws the reserve down (simulation_rules.apply_solvency) and never refills "
        "it. Listed dead rather than cited against the mapping line that merely parses it."),

    # ── other genuinely-dead assumptions.* leaves ──
    # (assumptions.rate_paths.* used to live here -- all five leaves. Issue #685
    # made them load-bearing: _reconcile_rate_paths reads every one of them to
    # detect a belief that contradicts a signed liability rate at year zero, and
    # the contradiction reaches the output as a model_fidelity Approximation.
    # See CONSUMED below. They do NOT set the rate the engine charges -- that is
    # liabilities[].rate, and #685 exists precisely because a rate_paths belief
    # used to silently override it.)
    "assumptions.retirement.drawdown_tax_mode": ("#579", "issue #579 deleted the gross-drawdown code "
        "path entirely -- the 'gross' vs 'net' switch has zero readers anywhere in the engine today "
        "(confirmed by grep); the schema keeps the field for documentation value only."),
    "assumptions.tax_law_overrides.contribution_limit_overrides.rrsp_annual_max": ("#603", "not "
        "mapped by to_internal_config -- the sensitivity-override mechanism this $def documents "
        "(overriding the year-versioned TaxDataProvider figure) is not yet wired from the contract; "
        "a real, narrow #599-adjacent follow-up, not this PR's job."),
    "assumptions.tax_law_overrides.contribution_limit_overrides.tfsa_annual_room": ("#603", "see "
        "assumptions.tax_law_overrides.contribution_limit_overrides.rrsp_annual_max above."),

    # ── portfolio.products.*.{category,mer,foreign_content,withholding_exposure,turnover} --
    # deliberately deferred (NOT re-verified as safe-to-delete in this PR), same reasoning Track C
    # Phase 2a gave: category has no default (Product.from_dict's data["category"] raises KeyError
    # without it) and countries/canada/input_schema.json (now schema/countries/canada/
    # input_schema.json) is loaded as a runnable config by real tests -- cutting the dataclass's
    # required-field contract is a separate, focused follow-up, not bundled into an already-large PR.
}
for _name in _EXAMPLE_PRODUCT_NAMES:
    for _field, _issue in (
        ("category", "#547"), ("mer", "#547"), ("foreign_content", "#547"),
        ("withholding_exposure", "#547"), ("turnover", "#547"),
    ):
        DEAD_ALLOWLIST[f"assumptions.products.{_name}.{_field}"] = (
            _issue,
            f"parsed onto Product.{_field}, zero non-test readers (independently re-verified, epic "
            f"#603 Phase 2b) -- turnover feeds realized_capital_gains/deferred_capital_gains, "
            f"themselves zero-caller properties. Deliberately NOT deleted in this PR -- category has "
            f"no default (Product.from_dict's data['category'] raises KeyError without it) and the "
            f"Canada overlay schema is loaded as a runnable config by real tests; cutting Product's "
            f"required-field contract is a separate, focused follow-up (same judgment Track C Phase "
            f"2a made, re-confirmed here rather than rushed alongside this PR's other ~30 findings).",
        )
# Issue #832: a private_loans[].lender.relationship leaf -- free-text label
# for an external lender, informational only, never consumed by the engine.
DEAD_ALLOWLIST["private_loans[].lender.relationship"] = (
    "#832", "free-text relationship of an external lender (e.g. 'grandparent', "
    "'friend') to the household. Informational only -- it records WHO the "
    "individual is without forcing them into people[]. The engine never "
    "consumes it: the lender's tax treatment keys on whether they are a "
    "household member (lender is a string vs an inline object), not on the "
    "relationship label. Deliberately NOT consumed, so the contract can carry "
    "human context the engine is structurally blind to (like provenance.* / "
    "sensitivity.sweeps.*).")


# ── Guard 1b: verified consumption citations ─────────────────────────────

CONSUMED = {
    # ── root-level ──
    "as_of": ("input_contract.py", "start_year = int(as_of[:4])"),

    # ── private_loans[] (issue #832: private loan from an individual) ──
    "private_loans[].id": ("input_contract.py", '"id": loan["id"]'),
    "private_loans[].lender": ("input_contract.py", 'lender = loan["lender"]'),
    "private_loans[].lender.id": ("input_contract.py", 'lender_id = lender.get("id")'),
    "private_loans[].borrower": ("input_contract.py", 'borrower = loan["borrower"]'),
    "private_loans[].rate": ("input_contract.py", '"rate": float(loan["rate"])'),
    "private_loans[].principal": ("input_contract.py", '"principal": float(loan["principal"])'),
    # epic #795 bite 4: the ITA payability/deductibility keywords moved out of
    # simulation.py into the Canada jurisdiction module (DP#10/DP#25).
    "private_loans[].use": ("countries/canada/private_loan_interest.py", "loan.get('use') == 'investment'"),
    "private_loans[].repayment": ("countries/canada/private_loan_interest.py", "loan.get('repayment') == 'amortizing'"),
    "private_loans[].interest": ("countries/canada/private_loan_interest.py", "loan.get('interest') == 'paid'"),

    # ── gifts[] (epic #841 bite 3: parent->child gift funds a child's room) ──
    "gifts[].id": ("input_contract.py", '"id": gift["id"]'),
    "gifts[].from": ("input_contract.py", 'donor = gift["from"]'),
    "gifts[].to": ("simulation_state.py", "by_child[g['to']]"),
    "gifts[].amount": ("simulation_state.py", "float(g['amount'])"),
    # Issue #859 (Part A): repayable marks a gift as an intra-family LOAN --
    # child_loan_funded_for_year sums only repayable gifts into the child's
    # loan_funded_principal (the receivable/liability on the family balance sheet).
    "gifts[].repayable": ("simulation_state.py", "g.get('repayable', False)"),
    "currency": ("model_fidelity.py", "currency = assumptions.get('currency', 'CAD')"),
    "dollars": ("model_fidelity.py", "dollar_basis = assumptions.get('dollar_basis', 'nominal')"),
    "jurisdiction.province": ("simulation.py", "'non_reg', primary_marginal_rate, province"),

    # ── people[] ──
    "people[].id": ("input_contract.py", "def _people_by_id(doc: Dict) -> Dict[str, Dict]"),
    "people[].label": ("simulation.py", "ch.get('name', 'Child')"),
    "people[].birth_date": ("simulation_rules.py", "m.get('birth_year', 0)"),
    "people[].relationships": ("input_contract.py", "for r in people[primary_id].get(\"relationships\", [])"),
    "people[].relationships[].type": ("input_contract.py", "if r[\"type\"] == \"spouse_of\""),
    "people[].relationships[].person": ("input_contract.py", "spouse_id = r[\"person\"]"),
    "people[].incomes": ("input_contract.py", "for inc in person.get(\"incomes\", []):"),
    "people[].incomes[].id": ("input_contract.py", "person_income_ids = {"),
    "people[].incomes[].kind": ("input_contract.py", "if inc[\"kind\"] != \"employment\":"),
    "people[].incomes[].amount": ("input_contract.py", "total += inc[\"amount\"]"),
    "people[].incomes[].from": ("input_contract.py", "if inc[\"from\"] and inc[\"from\"] > as_of:"),
    "people[].incomes[].to": ("input_contract.py", "if inc[\"to\"] and inc[\"to\"] < as_of:"),
    # Issue #767: employment-contract terms on an employment income. The
    # consumed scalar leaves drive the recovery-date clamp / notice-segment
    # model; scope/geography/probation_end are declared contract context that
    # reach no financial decision yet (see DEAD_ALLOWLIST). The container
    # objects `employment`/`non_compete` are not scalar leaves (the walker
    # treats them as containers), so only their scalar children are cited.
    "people[].incomes[].employment.non_compete.months": ("input_contract.py", 'return nc["months"]'),
    "people[].incomes[].employment.notice_days": ("input_contract.py", 'return emp.get("notice_days", 0)'),
    "people[].benefits.cpp.start_date": ("optimize.py", "cpp_start_age = primary.get('cpp_start_age'"),
    "people[].benefits.cpp.monthly_amount": ("optimize.py", "cpp_monthly_estimated = primary.get('cpp_monthly_estimated'"),
    "people[].benefits.oas.start_date": ("optimize.py", "oas_start_age = primary.get('oas_start_age'"),
    "people[].benefits.oas.defer_months": ("optimize.py", "oas_defer_months = primary.get('oas_defer_months'"),
    "people[].benefits.employer_pension.annual_amount": ("optimize.py", "pension_income_annual = primary.get('pension_income_annual'"),
    # #700/#643 (Steps 2/3): room now seeds the per-adult RRSP/TFSA stores.
    "people[].room.rrsp": ("simulation_state.py", "'own_room': primary.get('rrsp_room_accumulated'"),
    "people[].room.rrsp.contribution_room": ("simulation_state.py", "'own_room': primary.get('rrsp_room_accumulated'"),
    "people[].room.tfsa": ("simulation_state.py", "'room': primary.get('tfsa_room_accumulated'"),
    "people[].room.tfsa.contribution_room": ("simulation_state.py", "'room': primary.get('tfsa_room_accumulated'"),

    # ── accounts[] -- kind-blind literal entries deliberately NOT here
    # (#647): see the generated "accounts[kind=...]" block after this dict's
    # closing brace, and the lsif.*/lira.*/resp.* hand-written entries below
    # (single-kind leaves; a generator loop would buy nothing for them).
    # #700/#643/#704 (Step 4): LIRA/LIF now seed the per-adult stores.
    "accounts[kind=lira].lira.jurisdiction": ("simulation_state.py", "'jurisdiction': lira_cfg.get('jurisdiction'"),
    "accounts[kind=lif].lira.jurisdiction": ("simulation_state.py", "'jurisdiction': lira_cfg.get('jurisdiction'"),
    "accounts[kind=lira].lira.reference_rate": ("simulation_state.py", "'reference_rate': lira_cfg.get('reference_rate'"),
    "accounts[kind=lif].lira.reference_rate": ("simulation_state.py", "'reference_rate': lira_cfg.get('reference_rate'"),
    "accounts[kind=resp].resp.contributions_total": ("input_contract.py", "total_contrib = sum(a[\"resp\"][\"contributions_total\"] for a in resp_accounts)"),
    "accounts[kind=resp].resp.cesg_received": ("input_contract.py", "total_cesg = sum(a[\"resp\"][\"cesg_received\"] for a in resp_accounts)"),
    "accounts[kind=resp].resp.qesi_received": ("input_contract.py", "total_qesi = sum(a[\"resp\"][\"qesi_received\"] for a in resp_accounts)"),
    "accounts[kind=lsif].lsif.purchase_date": ("countries/canada/lsif_credit.py", "lsif.get(\"purchase_year\")"),
    "accounts[kind=lsif].lsif.purchase_province": ("countries/canada/lsif_credit.py", "is_quebec_resident=lsif.get(\"is_quebec_resident\""),
    "accounts[kind=lsif].lsif.federally_registered": ("countries/canada/lsif_credit.py", "federally_registered=lsif.get(\"federally_registered\""),
    "accounts[kind=lsif].lsif.prior_redemption": ("countries/canada/lsif_credit.py", "prior_redemption=lsif.get(\"prior_redemption\""),
    "accounts[kind=lsif].lsif.is_hbp_replacement": ("countries/canada/lsif_credit.py", "is_hbp_replacement=lsif.get(\"is_hbp_replacement\""),
    "accounts[kind=lsif].lsif.quebec_carryforward": ("countries/canada/lsif_credit.py", "quebec_carryforward=float(lsif.get(\"quebec_carryforward\""),
    "accounts[kind=lsif].lsif.acquisition_date": ("countries/canada/lsif_credit.py", "lsif.get(\"acquisition_date\")"),
    "accounts[kind=lsif].lsif.redeemed_date": ("countries/canada/lsif_credit.py", "lsif.get(\"redeemed_date\")"),
    "accounts[kind=lsif].lsif.reference_year_taxable_income": ("countries/canada/lsif_credit.py", "lsif.get(\"reference_year_taxable_income\")"),

    # ── estate.* + the designations that feed it (epic #603 Track C Phase 2c, #600).
    # These were the FIVE silent assumptions -- every one resolving in the favourable
    # direction -- that #600 was filed about. They are now real inputs: mapped by
    # input_contract._map_estate, turned into an EstatePlan by objective.plan_from_config,
    # and consumed by countries/canada/estate.py's compute_estate.
    "estate.default_spousal_rollover": ("input_contract.py", 'default_rollover = estate["default_spousal_rollover"]'),
    "estate.rollover_overrides[].account": ("input_contract.py", 'overrides = {o["account"]: o["spousal_rollover"]'),
    "estate.rollover_overrides[].spousal_rollover": ("input_contract.py", 'rolls = overrides[acc["id"]] if acc["id"] in overrides else default_rollover'),
    "estate.life_insurance[].insured": ("input_contract.py", 'if pol["insured"] not in couple:'),
    "estate.life_insurance[].face_amount": ("input_contract.py", 'death_benefit += pol["face_amount"]'),
    "estate.life_insurance[].term_end_date": ("input_contract.py", 'if term_end is not None and horizon_date is not None and term_end < horizon_date:'),
    "assumptions.mortality[].person": ("input_contract.py", 'mortality = {m["person"]: m for m in doc["assumptions"]["mortality"]}'),
    "assumptions.mortality[].assumed_death_age": ("input_contract.py", 'age = m.get("assumed_death_age")'),
    "assumptions.mortality[].assumed_death_date": ("input_contract.py", 'if m.get("assumed_death_date") is not None:'),
    "accounts[kind=tfsa].successor_holder": ("input_contract.py", 'a.get("successor_holder") is not None for a in couple_tfsas'),
    "accounts[kind=non_reg].owner.joint[].pct": ("input_contract.py", 'return {j["person"]: j["pct"] for j in owner["joint"]}'),
    "accounts[kind=non_reg].owner.joint[].person": ("input_contract.py", 'return {j["person"]: j["pct"] for j in owner["joint"]}'),
    "accounts[kind=resp].owner.joint[].person": ("input_contract.py", 'return {j["person"]: j["pct"] for j in owner["joint"]}'),
    "properties[].owner.joint[].pct": ("input_contract.py", 'shares = _owner_shares(prop["owner"])'),
    "properties[].owner.joint[].person": ("input_contract.py", 'shares = _owner_shares(prop["owner"])'),
    "properties[].designated_principal_residence_years": ("countries/canada/estate.py", 'if not plan.principal_residence_designated:'),
    # #695: the individual year ranges now move the tax -- pre_designation reads
    # each period's from/to to count a property's designated years, which sets its
    # per-property taxable fraction. (NOT_EXERCISED_BY_EXAMPLE in
    # test_contract_reachability.py: the shipped example's couple owns one
    # property, so these cannot move a dollar on that fixture.)
    "properties[].designated_principal_residence_years[].from": ("countries/canada/pre_designation.py", 'period["from"]'),
    "properties[].designated_principal_residence_years[].to": ("countries/canada/pre_designation.py", 'period["to"]'),

    # ── liabilities[] -- kind-blind entries deliberately NOT here (#654):
    # see the "liabilities[kind=...]" generated block below, after the
    # per-product loop.

    # ── properties[] ──
    "properties[].id": ("input_contract.py", "def _find_property(doc: Dict, kind: str)"),
    "properties[].kind": ("input_contract.py", "def _find_property(doc: Dict, kind: str)"),
    "properties[].value.amount": ("optimize.py", "config.house_value"),
    "properties[].acb": ("input_contract.py", 'other_acb += prop["acb"] * couple_share'),

    # ── cash_flows[] ──
    "cash_flows[].id": ("input_contract.py", "for cf in doc.get(\"cash_flows\", [])"),
    "cash_flows[].owner": ("input_contract.py", "for cf in doc.get(\"cash_flows\", [])"),
    "cash_flows[].date": ("input_contract.py", "\"year\": int(cf[\"date\"][:4])"),
    "cash_flows[].amount": ("input_contract.py", "\"amount\": cf[\"amount\"]"),
    "cash_flows[].tax_treatment": ("input_contract.py", "\"non-taxable\" if cf[\"tax_treatment\"] == \"tax_free\" else \"post-tax\""),
    "cash_flows[].description": ("input_contract.py", "for cf in doc.get(\"cash_flows\", [])"),

    # ── assumptions.* (universal beliefs) ──
    "assumptions.default_non_reg_yield": ("input_contract.py", "default_yield = assumptions[\"default_non_reg_yield\"]"),
    "assumptions.inflation": ("simulation.py", "self.config.inflation is not None"),
    "assumptions.salary_growth": ("simulation.py", "cfg.salary_growth"),
    "assumptions.savings_rate": ("simulation.py", "annual_savings = total_income * cfg.savings_rate"),
    "assumptions.time_step": ("simulation.py", "self.config.time_step =="),
    "assumptions.tax_law_overrides.capital_gains_inclusion": ("optimize.py", "config.capital_gains_inclusion"),
    "assumptions.tax_law_overrides.frozen_brackets": ("simulation.py", "self.config.frozen_brackets"),
    "assumptions.tax_law_overrides.oas.disabled": ("input_contract.py", "retirement_out[\"oas_annual_max\"] = 0"),
    "assumptions.tax_law_overrides.oas.annual_max_override": ("input_contract.py", "retirement_out[\"oas_annual_max\"] = oas_override[\"annual_max_override\"]"),
    # ── assumptions.rate_paths.* (issue #685) ──
    # Reconciled against the SIGNED rate on the matching liability, never used to
    # set it. `.type` selects which leaf carries year zero (`.rate` for fixed,
    # `.path[0]` for variable/forecast), and a disagreement with
    # liabilities[kind=X].rate is warned about at load and surfaced as a
    # model_fidelity Approximation naming both figures.
    # (Only the leaves the example DOCUMENT actually instantiates are enumerated:
    # its mortgage path is `fixed` -- .type/.rate -- and its heloc path is
    # `variable` -- .type/.path. `_rate_path_year0` reads whichever of .rate /
    # .path[0] the declared .type selects, so all four are load-bearing.)
    "assumptions.rate_paths.mortgage.type": ("input_contract.py", "if path[\"type\"] == \"fixed\":"),
    "assumptions.rate_paths.mortgage.rate": ("input_contract.py", "return path[\"rate\"]"),
    "assumptions.rate_paths.heloc.type": ("input_contract.py", "if path[\"type\"] == \"fixed\":"),
    "assumptions.rate_paths.heloc.path": ("input_contract.py", "series = path[\"path\"]"),
    "assumptions.retirement.spending_target": ("input_contract.py", "if retirement_cfg.get(\"spending_target\") is not None:"),
    "household_budget.annual_living_costs": ("input_contract.py", "if household_budget_cfg.get(\"annual_living_costs\") is not None:"),
    # Issue #761: the discretionary/non-discretionary split. Cited to the
    # ENGINE consumer (simulation_rules.apply_solvency), not the pure loader
    # (simulation_config.py is excluded by test_consumed_citations_avoid_the_pure_loader)
    # and not only the adapter -- a leaf read by input_contract is not the
    # same as the block reaching the engine (#665). apply_solvency reads
    # ctx.config.discretionary_fraction to compress the discretionary portion
    # to zero under a dated income shock, so the value reaches a real decision.
    "household_budget.discretionary_fraction": ("simulation_rules.py", "frac = ctx.config.discretionary_fraction"),
    # Issue #760: dated, finite-term living-cost segments. Each field is cited
    # to the ENGINE consumer (simulation_rules.apply_solvency + the pure
    # day-count helper it calls), not the pure loader (simulation_config.py is
    # excluded) -- the value reaches a real decision, not merely a copied key.
    # `amount`/`from`/`to` drive the day-count blend that decides the year's
    # charged outflow; `non_discretionary` decides whether the segment
    # compresses under an income shock. `description` is a free-text record
    # (like cash_flows[].description above) whose terminal is the adapter
    # mapping.
    "household_budget.expense_segments[].description": ("input_contract.py", '"description": seg["description"],'),
    "household_budget.expense_segments[].amount": ("simulation_rules.py", "return segment['amount'] * days_active / days_in_year"),
    "household_budget.expense_segments[].from": ("simulation_rules.py", "start = segment['from']"),
    "household_budget.expense_segments[].to": ("simulation_rules.py", "end = segment['to']"),
    "household_budget.expense_segments[].non_discretionary": ("simulation_rules.py", "if _seg['non_discretionary']:"),
    # ── assumptions.emergency_reserve (issue #688) ──
    "assumptions.emergency_reserve.target_months": ("input_contract.py", '"target_months": reserve_cfg["target_months"],'),
    "assumptions.emergency_reserve.rate": ("input_contract.py", '"rate": reserve_cfg["rate"],'),
    "assumptions.emergency_reserve.instrument": ("input_contract.py", '"instrument": reserve_cfg["instrument"],'),
    "assumptions.emergency_reserve.held_in": ("input_contract.py", 'held_in_kind = hosts[0]["kind"]'),
    "assumptions.retirement.drawdown_order": ("simulation_rules.py", "drawdown_order = ret.get('drawdown_order')"),
    "assumptions.retirement.net_replacement_rate": ("simulation_rules.py", "net_replacement_rate = ret.get('net_replacement_rate'"),
    "assumptions.return_model.type": ("return_model.py", "rtype = data.get(\"type\", \"fixed\")"),
    "assumptions.return_model.rate": ("return_model.py", "rate=data.get(\"rate\", 0.07)"),

    # ── Canada overlay: assumptions.resp (issue #578) ──
    "assumptions.resp.eap_tax_rate": ("countries/canada/resp_rules.py", "student_mtr = base_cfg.get('assumptions', {}).get('resp_eap_tax_rate'"),
    "assumptions.resp.eap_taxable_portion": ("countries/canada/resp_rules.py", "student_mtr = base_cfg.get('assumptions', {}).get('resp_eap_tax_rate'"),
    # #714 moved these call sites: the window is now computed ONCE per child by
    # resp_study_window_for_child(), which prefers the child's DECLARED
    # study_periods and falls back to these household-wide age assumptions only
    # when nothing was declared (DP#13). They are still genuinely consumed --
    # as the fallback, and as the duration that closes an open-ended window.
    "assumptions.resp.study_start_age": ("simulation_rules.py", "ctx.config.resp_study_start_age, ctx.config.resp_study_duration_years)"),
    "assumptions.resp.study_duration_years": ("simulation_rules.py", "ctx.config.resp_study_start_age, ctx.config.resp_study_duration_years)"),
    "assumptions.resp.used_for_education": ("simulation_rules.py", "if ctx.config.resp_used_for_education and in_study_year:"),

    # ── decisions.* (universal, DP#5 anchor decisions) ──
    "decisions.horizon.person": ("input_contract.py", "def _find_primary_and_spouse(doc: Dict)"),
    "decisions.horizon.until_age": ("input_contract.py", "assumptions_cfg[\"horizon_age\"] = horizon[\"until_age\"]"),
    "decisions.retirement_age[].person": ("input_contract.py", "if cand[\"person\"] == person_id and cand[\"candidate_ages\"]:"),
    "decisions.income[].id": ("scenario_discovery.py", "def _convert_income_scenarios"),
    "decisions.income[].label": ("scenario_discovery.py", "def _convert_income_scenarios"),
    "decisions.income[].overrides": ("input_contract.py", "sc[\"overrides\"], people_by_id, person_income_ids"),
    "decisions.income[].overrides[].income_id": ("input_contract.py", "owner_id = person_income_ids.get(ov[\"income_id\"])"),
    "decisions.income[].overrides[].amount": ("input_contract.py", "\"gross_income\": ov[\"amount\"],"),
    # ── issue #674: kind/from/to travel with the amount now (dated,
    # kind-classified income shocks -- see simulation.py's
    # _income_components_for_year and simulation_rules.py's
    # apply_contribution_room, ITA s.146(1)) ──
    "decisions.income[].overrides[].kind": ("input_contract.py", "\"kind\": ov[\"kind\"],"),
    "decisions.income[].overrides[].from": ("input_contract.py", "\"from\": ov[\"from\"],"),
    "decisions.income[].overrides[].to": ("input_contract.py", "\"to\": ov[\"to\"],"),
    "decisions.mortgage.refinance_options[].id": ("scenario_discovery.py", "'id': scenario.get('id', 'unknown')"),
    "decisions.mortgage.refinance_options[].label": ("scenario_discovery.py", "'label': scenario.get('label', 'Refinance scenario')"),
    "decisions.mortgage.refinance_options[].cash_out": ("scenario_discovery.py", "cash_out = scenario.get('cash_out', 0)"),
    "decisions.mortgage.refinance_options[].amortization_years": ("input_contract.py",
        "prop_cfg[\"refinance_amortization_years\"] = refinance_options[0][\"amortization_years\"]"),
    "decisions.mortgage.refinance_options[].advance_split.deductible_non_reg": ("input_contract.py",
        "split_option[\"advance_split\"][\"deductible_non_reg\"]"),
    "decisions.mortgage.renewal_options[].label": ("optimize.py", "name = opt.get('name', f\"renew_{opt.get('type', 'unknown')}\")"),
    "decisions.mortgage.renewal_options[].rate": ("scenario_discovery.py", "'rate': option.get('rate', 0.05)"),
    "decisions.mortgage.renewal_options[].type": ("scenario_discovery.py", "'type': option.get('type', 'variable')"),
    "decisions.mortgage.renewal_options[].term_years": ("scenario_discovery.py", "'term_years': option.get('term_years', 5)"),
    # ── decisions.mortgage.structure_options[] -- issue #687. Mapped by
    # input_contract.py onto property.structure_options; the actual charge
    # split (revolving_share -> mortgage_balance/margin_available) is real
    # decision logic in simulation_config.apply_structure_overlay (excluded
    # from citation here as the pure-loader file, same as every other
    # leaf), so these citations point at the two REAL consumers instead:
    # input_contract.py's own mapping/refusal logic (a genuine decision --
    # whether a structure can be priced, DP#32/#654), and optimize.py's
    # sweep, which tags every ranked row with the structure's own fields
    # (the ranking IS the decision this data reaches).
    "decisions.mortgage.structure_options[].id": ("input_contract.py", '"id": opt["id"], "label": opt["label"]'),
    "decisions.mortgage.structure_options[].label": ("input_contract.py", '"id": opt["id"], "label": opt["label"]'),
    "decisions.mortgage.structure_options[].revolving_share": ("optimize.py", "structure.get('revolving_share')"),
    "decisions.mortgage.structure_options[].readvanceable": ("optimize.py", "structure.get('readvanceable')"),
    "decisions.mortgage.structure_options[].revolving_rate": ("input_contract.py", 'revolving_rate = opt.get("revolving_rate")'),
    # ── decisions.contribution_strategy[] -- issue #713. The block was parsed
    # by the schema and dropped by the adapter, so a household's OWN authored
    # savings strategies never reached the optimizer: the ranked table they got
    # back ranked the engine's built-in `balanced`/`rrsp_max` instead. Now
    # mapped onto cfg['strategies'] and handed to discover_strategies(
    # custom_strategies=...) -- the hook that always existed and was never
    # passed anything. Citations point at where each lever reaches a DECISION,
    # not at the mapper that copies it.
    "decisions.contribution_strategy[].id": ("optimize.py", "custom_strategies=cfg.get('strategies')"),
    "decisions.contribution_strategy[].label": ("optimize.py", "custom_strategies=cfg.get('strategies')"),
    "decisions.contribution_strategy[].allocation.rrsp_pct": ("strategy.py", "s.rrsp_pct * savings"),
    "decisions.contribution_strategy[].allocation.spousal_rrsp_pct": ("strategy.py", "s.spousal_rrsp_pct * savings"),
    "decisions.contribution_strategy[].allocation.resp_pct": ("strategy.py", "s.resp_pct * savings"),
    "decisions.contribution_strategy[].allocation.fhsa_pct": ("strategy.py", "s.fhsa_pct * savings"),
    # #751: the last two allocation leaves. allocate() used to fund TFSA at a
    # hardcoded `remaining * 0.5` and sweep the rest to non-reg, so a declared
    # tfsa_pct/non_reg_pct reached AllocationStrategy (via #713) and was never
    # spent. tfsa_pct is now spent directly as the TFSA target. non_reg_pct is
    # honoured as the residual: the six contract percentages sum to ~1.0
    # (total_pct/validate), the five registered targets are spent as
    # `s.<x>_pct * savings` (capped by room), so `remaining` allocated to
    # non-reg equals `non_reg_pct * savings` when registered room is sufficient
    # and absorbs the room-shortfall spill otherwise -- the field's stated
    # purpose ("remainder goes here"). The citation is the allocation decision
    # line where the residual materialises, not a `total_pct`/`from_dict`
    # naming line (citing those would be the "named is not spent" lie #751
    # warns about).
    "decisions.contribution_strategy[].allocation.tfsa_pct": ("strategy.py", "s.tfsa_pct * savings"),
    "decisions.contribution_strategy[].allocation.non_reg_pct": ("strategy.py", "result.non_reg = max(0, remaining)"),
    "decisions.contribution_strategy[].use_smith": ("optimize.py", "use_readvanceable = strategy.prioritize_readvanceable"),
    "decisions.contribution_strategy[].deduct_later": ("optimize.py", "deduct_later = strategy.deduct_later"),
    # The bracket-fill target is the one lever the engine reads from the config
    # rather than the strategy (SimulationConfig.deduct_later_bracket_target),
    # so the adapter lands it there too -- see input_contract.py's comment.
    "decisions.contribution_strategy[].deduct_later_bracket_target": (
        "simulation_rules.py", "bracket_target=ctx.config.deduct_later_bracket_target,"),

    # ── people[].study_periods[] -- issue #714. Mapped into
    # child['study_periods'] and read by NOBODY: every beneficiary's RESP wound
    # down on the GLOBAL assumptions.resp.study_start_age, so a child who
    # starts at 19 (or studies six years) had their EAP/AIP schedule computed
    # against a window they never declared. DP#1/DP#28.
    "people[].study_periods[].start_date": ("countries/canada/resp_rules.py",
        "starts = [p['start_year'] for p in periods if p.get('start_year') is not None]"),
    "people[].study_periods[].end_date": ("countries/canada/resp_rules.py",
        "p['end_year'] if p.get('end_year') is not None"),
    "decisions.resp_action[].id": ("scenario_discovery.py", "{'id': item['id']} for item in resp_action_scenarios"),

    # ── sensitivity.presets (universal) ──
    "sensitivity.presets.conservative.investment_return": ("optimize.py", "if 'investment_return' in overlay"),
    "sensitivity.presets.conservative.salary_growth": ("optimize.py", "'salary_growth' in overlay"),
    "sensitivity.presets.conservative.inflation": ("optimize.py", "cfg.setdefault('assumptions', {})['inflation'] = overlay['inflation']"),
    "sensitivity.presets.conservative.label": ("optimize.py", "overlay_info.get('label'"),
    "sensitivity.presets.moderate.investment_return": ("optimize.py", "if 'investment_return' in overlay"),
    "sensitivity.presets.moderate.salary_growth": ("optimize.py", "'salary_growth' in overlay"),
    "sensitivity.presets.moderate.inflation": ("optimize.py", "cfg.setdefault('assumptions', {})['inflation'] = overlay['inflation']"),
    "sensitivity.presets.moderate.label": ("optimize.py", "overlay_info.get('label'"),
    "sensitivity.presets.aggressive.investment_return": ("optimize.py", "if 'investment_return' in overlay"),
    "sensitivity.presets.aggressive.salary_growth": ("optimize.py", "'salary_growth' in overlay"),
    "sensitivity.presets.aggressive.inflation": ("optimize.py", "cfg.setdefault('assumptions', {})['inflation'] = overlay['inflation']"),
    "sensitivity.presets.aggressive.label": ("optimize.py", "overlay_info.get('label'"),

    # ── sensitivity.sweeps (issue #771) -- WIRED. Previously #593-dead (the
    # legacy sensitivity_overlays.* keys these targeted had zero real consumers);
    # now GENERAL contract-path sweeps consumed by sweep.py, which sets the
    # declared leaf to each value, re-maps via to_internal_config, runs the
    # optimizer, and reports the objective + first-shortfall year per value. The
    # three legacy short-names are sugar over the same resolver (DP#9): each is an
    # alias for the canonical contract path it always meant, expanded in
    # sweep._expand_axis. The general path key IS a real contract leaf address,
    # read by sweep.resolve_leaf and set on the mapped run.
    "sensitivity.sweeps.investment_return": ("sweep.py", '"investment_return": "assumptions.return_model.rate"'),
    "sensitivity.sweeps.mortgage_rate": ("sweep.py", 'axis == "mortgage_rate"'),
    "sensitivity.sweeps.savings_rate": ("sweep.py", '"savings_rate": "assumptions.savings_rate"'),
    "sensitivity.sweeps.assumptions.retirement.spending_target": (
        "sweep.py", "def resolve_leaf"),
}
for _name in _EXAMPLE_PRODUCT_NAMES:
    for _field, (_file, _kw) in {
        "us_equity_pct": ("countries/canada/product_registry.py", "us_equity_pct=data.get(\"us_equity_pct\""),
        "intl_equity_pct": ("countries/canada/product_registry.py", "intl_equity_pct=data.get(\"intl_equity_pct\""),
        "foreign_income": ("countries/canada/product_registry.py", "foreign_income=data.get(\"foreign_income\""),
        "capital_gains": ("countries/canada/product_registry.py", "capital_gains=data.get(\"capital_gains\""),
    }.items():
        CONSUMED[f"assumptions.products.{_name}.{_field}"] = (_file, _kw)


# ── accounts[kind=...] -- generated, kind-aware coverage (issue #647) ────
#
# The walker tags every accounts[] leaf with its own kind (see _iter_leaves
# above), so the leaves below are named "accounts[kind=X].field", never a
# bare "accounts[].field" that would silently re-certify all twelve kinds
# from one citation -- the exact blindness that let a single kind=rrsp
# consumer vouch for spousal_rrsp/fhsa while they dropped $72,000 (#647).
#
# Generated from _map_registered_balances' own three-way split
# (input_contract.py) rather than hand-duplicated per kind, so this
# registry cannot silently drift out of sync with what the mapper actually
# does with each kind.
_EXAMPLE_ACCOUNT_KINDS = sorted({a["kind"] for a in json.loads(EXAMPLE_PATH.read_text())["accounts"]})

# Four kinds (rrsp/tfsa/fhsa/spousal_rrsp) are mapped per-owner by
# _map_registered_balances itself; the other five (non_reg/resp/lira/lif/
# lsif) are aggregated by dedicated, separately-cited logic further down in
# to_internal_config -- both groups are covered by the citation dicts below.

# kind.balance.amount citation -- one line per kind, the exact place its
# balance reaches the internal shape (verified present by
# test_consumed_citations_are_still_true, same as every other CONSUMED entry).
_BALANCE_AMOUNT_CITATION = {
    "rrsp": ("input_contract.py", "balances[owner][field] = balances[owner].get(field, 0) + amount"),
    "tfsa": ("input_contract.py", "balances[owner][field] = balances[owner].get(field, 0) + amount"),
    "spousal_rrsp": ("input_contract.py", 'balances[spouse_id]["spousal_rrsp_balance"] = ('),
    "fhsa": ("input_contract.py", 'balances[owner]["fhsa_balance"] = balances[owner].get("fhsa_balance", 0) + amount'),
    "non_reg": ("input_contract.py", '"balance": sum(a["balance"]["amount"] for a in non_reg_accounts),'),
    "resp": ("input_contract.py", 'resp_balance = sum(a["balance"]["amount"] for a in resp_accounts)'),
    "lira": ("input_contract.py", '"balance": sum(a["balance"]["amount"] for a in lira_accounts),'),
    "lif": ("input_contract.py", '"balance": sum(a["balance"]["amount"] for a in lira_accounts),'),
    "lsif": ("input_contract.py", 'purchase_amount = acc["balance"]["amount"]'),
}

# kind.owner citation -- individual/registered kinds only (non_reg/resp use
# owner.joint[] instead, cited separately above).
_OWNER_CITATION = {
    "rrsp": ("input_contract.py", 'owner = acc.get("owner")'),
    "tfsa": ("input_contract.py", 'owner = acc.get("owner")'),
    "spousal_rrsp": ("input_contract.py", 'owner = acc.get("owner")'),
    "fhsa": ("input_contract.py", 'owner = acc.get("owner")'),
    "lira": ("input_contract.py", '_people_by_id(doc)[first["owner"]]'),
    "lif": ("input_contract.py", '_people_by_id(doc)[first["owner"]]'),
    "lsif": ("input_contract.py", 'owner = _people_by_id(doc)[acc["owner"]]'),
    # non_reg's owner may be a bare person id (single-owner) or a
    # {"joint": [...]} split (example.json's non_reg accounts include both
    # shapes) -- _owner_shares handles either, used by _ownership_share's
    # non_reg-primary-share computation (_map_estate).
    "non_reg": ("input_contract.py", 'shares = _owner_shares(acc["owner"])'),
}

# kinds whose accounts[].id feeds the estate rollover-fraction lookup
# (_weighted_rolled_fraction's `acc["id"] in overrides`) -- _REGISTERED_KINDS
# plus non_reg (the two calls in _map_estate).
_ROLLOVER_ID_KINDS = {"rrsp", "spousal_rrsp", "rrif", "lira", "lif", "dcpp", "dbpp", "lsif", "non_reg"}

for _kind in _EXAMPLE_ACCOUNT_KINDS:
    CONSUMED[f"accounts[kind={_kind}].kind"] = ("input_contract.py", 'kind = acc["kind"]')
    DEAD_ALLOWLIST[f"accounts[kind={_kind}].balance.as_of"] = ("#597", "the per-account statement "
        "date is parsed (dated_amount) but only .amount is read -- the engine has one global as_of "
        "(the document root), not per-balance dating.")
    DEAD_ALLOWLIST[f"accounts[kind={_kind}].beneficiary"] = ("#600", "CONSUMED for TFSAs? No -- "
        "honestly, no. Phase 2c reads `successor_holder` (which decides whether the shelter "
        "survives) but NOT `beneficiary`: under the ITA the death-date VALUE of every account "
        "reaches the named beneficiary regardless of who they are, and this engine's estate is a "
        "single household pot, not a per-heir distribution. Routing dollars to individual heirs is "
        "a real feature (it is what would make `beneficiary` load-bearing) and is not built yet.")

    if _kind in _BALANCE_AMOUNT_CITATION:
        CONSUMED[f"accounts[kind={_kind}].balance.amount"] = _BALANCE_AMOUNT_CITATION[_kind]
    if _kind in _OWNER_CITATION:
        CONSUMED[f"accounts[kind={_kind}].owner"] = _OWNER_CITATION[_kind]

    if _kind in _ROLLOVER_ID_KINDS:
        CONSUMED[f"accounts[kind={_kind}].id"] = ("input_contract.py",
            'rolls = overrides[acc["id"]] if acc["id"] in overrides else default_rollover')
    else:
        DEAD_ALLOWLIST[f"accounts[kind={_kind}].id"] = ("#600", "`.id` is read by "
            "_weighted_rolled_fraction (the estate rollover-fraction lookup) only for kinds in "
            "_REGISTERED_KINDS plus non_reg -- this kind doesn't participate in that mechanism "
            "(TFSA's own shelter-continuation decision is driven by `successor_holder` directly, "
            "not this id lookup; RESP/FHSA aren't rollover-fraction-eligible kinds in this model) "
            "-- `.id` parses but reaches no decision.")

    if _kind == "non_reg":
        CONSUMED[f"accounts[kind={_kind}].acb"] = ("input_contract.py", '"cost_basis": sum(declared_acbs)')
        CONSUMED[f"accounts[kind={_kind}].holdings[].product"] = (
            "countries/canada/portfolio.py", "product = registry.get(holding['product'])")
        CONSUMED[f"accounts[kind={_kind}].holdings[].weight"] = (
            "countries/canada/portfolio.py", "weight = holding.get('weight', default_weight)")
    else:
        DEAD_ALLOWLIST[f"accounts[kind={_kind}].acb"] = ("#647", "acb is parsed for every kind (a "
            "base account field) but only non_reg's is ever read (portfolio_cfg's cost_basis sum) "
            "-- a registered/individual account is tax-sheltered, so its cost basis never feeds a "
            "capital-gains calculation; genuinely, correctly, never consumed for this kind.")
        DEAD_ALLOWLIST[f"accounts[kind={_kind}].holdings[].product"] = ("#641", "holdings[] is "
            "parsed by the schema for every account kind, but to_internal_config only ever maps "
            "non_reg's holdings into portfolio_cfg (simulation.py's only consumer, "
            "`portfolio.accounts.get('non_reg')`, never reads any other kind) -- confirmed blocker "
            "for asset location/allocation (#473/#474), independently re-confirmed by this "
            "kind-aware guard.")
        DEAD_ALLOWLIST[f"accounts[kind={_kind}].holdings[].weight"] = ("#641",
            "see accounts[kind=" + _kind + "].holdings[].product above -- same non-consumption.")

    if _kind == "tfsa":
        CONSUMED[f"accounts[kind={_kind}].successor_holder"] = (
            "input_contract.py", 'a.get("successor_holder") is not None for a in couple_tfsas')
    else:
        DEAD_ALLOWLIST[f"accounts[kind={_kind}].successor_holder"] = ("#600", "`couple_tfsas` "
            "(the only reader of successor_holder) filters to kind=tfsa specifically -- "
            "successor_holder on any other kind is parsed but never read (this account kind's own "
            "shelter-continuation, if it has one, isn't modelled the same way TFSA's is).")


# ── liabilities[kind=...] -- generated, kind-aware coverage (issue #654) ──
#
# The walker tags every liabilities[] leaf with its own kind (see
# _iter_leaves above, extended by #654 the same way #647 extended it for
# accounts[]), so the leaves below are named "liabilities[kind=X].field",
# never a bare "liabilities[].field" that would silently re-certify every
# kind in the example (mortgage/heloc/car_loan) from one citation -- the
# exact blindness that let kind=heloc's `.rate` hide behind kind=mortgage's
# citation for as long as it did (property.mortgage_rate was the ONLY rate
# the engine's HELOC interest path ever actually read, #654).
_EXAMPLE_LIABILITY_KINDS = sorted({l["kind"] for l in json.loads(EXAMPLE_PATH.read_text())["liabilities"]})

# `.id` is parsed for every kind but never actually read: _find_liability
# (input_contract.py) matches liabilities by `kind` (and, for mortgage/
# heloc, by `collateral`) -- never by `id` -- and no other reader touches
# a liability's own id either (confirmed by grep). Genuinely dead for
# every kind, not merely "not yet wired" for any one of them.
for _kind in _EXAMPLE_LIABILITY_KINDS:
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].id"] = ("#654", "_find_liability matches "
        "liabilities by `kind` (and `collateral`, for mortgage/heloc) -- never by `id`, and no "
        "other reader touches a liability's own id (confirmed by grep). Parsed, never read, for "
        "every kind.")
    # `.kind` IS read for every liability record, regardless of which kind: _find_liability
    # comprehends doc['liabilities'] evaluating `liab["kind"] == kind` for each one while
    # collecting a specific kind (#652: it collects ALL matches and refuses on >1, rather than
    # returning the first) -- car_loan's own `.kind` is evaluated (and rejected) exactly the same
    # way mortgage's/heloc's are matched. The discriminator is always consulted.
    CONSUMED[f"liabilities[kind={_kind}].kind"] = ("input_contract.py", 'if liab["kind"] == kind')
    # `.balance.as_of` -- only the document's one global `as_of` dates the simulation; no
    # per-liability statement date is read, for any kind (same reasoning as accounts[].balance.as_of).
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].balance.as_of"] = ("#597", "only the document's "
        "global as_of dates the simulation, not per-liability statement dates -- same reasoning "
        "as accounts[].balance.as_of, for every liability kind.")
    # `.owner` -- _find_liability/_find_property match by `kind` (+ `collateral`) only; owner is
    # parsed but never read for filtering or attribution, for any kind (#601 follow-up). Only a
    # leaf in its own right for kinds whose example owner is a bare person id (a joint-split
    # owner recurses into owner.joint[].pct/.person leaves instead -- see mortgage below).
    _liab_example = next(l for l in json.loads(EXAMPLE_PATH.read_text())["liabilities"] if l["kind"] == _kind)
    if not isinstance(_liab_example["owner"], dict):
        DEAD_ALLOWLIST[f"liabilities[kind={_kind}].owner"] = ("#601", "_find_liability matches by "
            "`kind` (+ `collateral` for mortgage/heloc) only -- owner is parsed but never read "
            "for filtering or attribution. The engine's mortgage/HELOC/other-debt state is "
            "household-level, not owner-attributed, in Phase 2b (a real #601 follow-up).")

if "mortgage" in _EXAMPLE_LIABILITY_KINDS:
    # mortgage's `owner` in the example is a joint split -- both leaves exist only for this kind.
    DEAD_ALLOWLIST["liabilities[kind=mortgage].owner.joint[].pct"] = (
        "#601", "_find_liability matches by `kind` (+ `collateral`) only -- owner is parsed but "
        "never read for filtering or attribution (same finding as the bare .owner leaf for "
        "kinds whose owner is not a joint split).")
    DEAD_ALLOWLIST["liabilities[kind=mortgage].owner.joint[].person"] = (
        "#601", "see liabilities[kind=mortgage].owner.joint[].pct above.")
    # `.collateral` IS read for mortgage: _find_liability(doc, "mortgage", principal["id"]) filters
    # by it (falling back to an unfiltered lookup if that returns None).
    CONSUMED["liabilities[kind=mortgage].collateral"] = (
        "input_contract.py", 'liab.get("collateral") == collateral_id')
    CONSUMED["liabilities[kind=mortgage].balance.amount"] = (
        "input_contract.py", 'prop_cfg["mortgage_balance"] = mortgage["balance"]["amount"]')
    CONSUMED["liabilities[kind=mortgage].rate"] = (
        "input_contract.py", 'prop_cfg["mortgage_rate"] = mortgage["rate"]')
    # mortgage's own rate_type is parsed but never read: the engine's rate path is built from
    # config.mortgage_rate alone (simulation.py) -- there is no "is this mortgage fixed or
    # variable" branch anywhere downstream of the mapping.
    DEAD_ALLOWLIST["liabilities[kind=mortgage].rate_type"] = ("#603", "the mortgage rate path "
        "(simulation.py's build_rate_path) is built from config.mortgage_rate alone -- "
        "rate_type is parsed but never read for the mortgage either.")
    CONSUMED["liabilities[kind=mortgage].amortization.years"] = (
        "input_contract.py", 'prop_cfg["amortization_years"] = mortgage["amortization"]["years"]')
    DEAD_ALLOWLIST["liabilities[kind=mortgage].amortization.payment_monthly"] = ("#603",
        "maps to legacy property.current_payment_monthly, confirmed dead and deleted from the "
        "schema in Track C Phase 2a; still dead now (SimulationConfig.from_dict never reads "
        "prop.get('current_payment_monthly')).")
    DEAD_ALLOWLIST["liabilities[kind=mortgage].renewal_date"] = ("#603", "maps to legacy "
        "property.renewal_date, confirmed dead and deleted from the schema in Track C Phase 2a; "
        "still dead now (SimulationConfig.from_dict never reads prop.get('renewal_date')).")
    DEAD_ALLOWLIST["liabilities[kind=mortgage].term_start_date"] = ("#603", "maps to legacy "
        "property.contract_start_date -- same finding as liabilities[kind=mortgage].renewal_date.")

if "heloc" in _EXAMPLE_LIABILITY_KINDS:
    CONSUMED["liabilities[kind=heloc].collateral"] = (
        "input_contract.py", 'liab.get("collateral") == collateral_id')
    CONSUMED["liabilities[kind=heloc].limit"] = (
        "input_contract.py", 'prop_cfg["margin_available"] = heloc["limit"]')
    CONSUMED["liabilities[kind=heloc].readvanceable"] = (
        "input_contract.py", 'prop_cfg["heloc_readvance"] = heloc.get("readvanceable", False)')
    # issue #577: the heloc's own opening/DRAWN balance never reaches SimState.initial() --
    # a draw is a simulation decision (FamilySimulation's lump_sum handling), never a fact
    # read off this field. See tests/architecture/test_contract_reachability.py.
    DEAD_ALLOWLIST["liabilities[kind=heloc].balance.amount"] = ("#577", "the DRAWN heloc balance "
        "has no legacy home: SimState.initial() always starts heloc_balance at 0 (undrawn) by "
        "design, regardless of any declared opening balance -- a draw is a simulation decision "
        "made elsewhere (FamilySimulation's lump_sum handling), never a fact read off this field.")
    # issue #654: THE fix. heloc.rate now maps to property.heloc_rate -> SimulationConfig.
    # heloc_rate -> FamilySimulation's heloc_path, which honours it outright instead of
    # deriving a HELOC rate from the mortgage's rate path (the bug this issue closes).
    CONSUMED["liabilities[kind=heloc].rate"] = (
        "simulation.py", "self.heloc_path = adapter.build_heloc_path(self.rate_path, heloc_rate=config.heloc_rate)")
    # heloc.rate_type is mapped alongside .rate for round-trip completeness (DP#24) and future
    # rate-path modelling, but is not YET load-bearing: HELOCPath.get_heloc_rate returns
    # fixed_rate outright once declared, for every year, regardless of mortgage_type/rate_type --
    # there is no year-over-year HELOC rate PATH in this engine yet for "variable" to switch on.
    DEAD_ALLOWLIST["liabilities[kind=heloc].rate_type"] = ("#654", "mapped to property."
        "heloc_rate_type for round-trip completeness, but HELOCPath.get_heloc_rate returns "
        "fixed_rate outright once a HELOC rate is declared, for every year, regardless of "
        "mortgage_type -- there is no year-over-year HELOC rate PATH in this engine for "
        "'variable' vs 'fixed' to switch on yet (that would be assumptions.rate_paths.heloc's "
        "job, which is itself unwired end-to-end -- see input_contract.py's heloc mapping "
        "comment). Genuinely parsed, not yet consumed.")
    DEAD_ALLOWLIST["liabilities[kind=heloc].capitalize_interest"] = ("#603", "legacy[\"heloc\"] "
        "(the only place this was mapped to) confirmed to have ZERO production readers and "
        "deleted from to_internal_config in epic #603 -- see input_contract.py's module "
        "docstring findings section.")
    DEAD_ALLOWLIST["liabilities[kind=heloc].deductibility.investment_portion"] = ("#603",
        "see liabilities[kind=heloc].capitalize_interest above -- same non-consumption.")
    DEAD_ALLOWLIST["liabilities[kind=heloc].deductibility.personal_portion"] = ("#603",
        "see liabilities[kind=heloc].capitalize_interest above -- same non-consumption.")

if "line_of_credit" in _EXAMPLE_LIABILITY_KINDS:
    # Issue #689: THE fix -- a revolving, unsecured (or secured) credit
    # facility, distinct from heloc, now has a real legacy home
    # (SimulationConfig.credit_facility_limit/_rate/_rate_type/_secured).
    CONSUMED["liabilities[kind=line_of_credit].collateral"] = (
        "input_contract.py", 'credit_facility.get("collateral") == principal["id"]')
    CONSUMED["liabilities[kind=line_of_credit].limit"] = (
        "input_contract.py", 'prop_cfg["credit_facility_limit"] = credit_facility["limit"]')
    CONSUMED["liabilities[kind=line_of_credit].rate"] = (
        "input_contract.py", 'prop_cfg["credit_facility_rate"] = credit_facility["rate"]')
    # .rate_type is mapped (property.credit_facility_rate_type ->
    # SimulationConfig.credit_facility_rate_type) for round-trip
    # completeness (DP#24), same reasoning as liabilities[kind=heloc]
    # .rate_type above: this engine prices a revolving facility off a single
    # current-year scalar rate either way (apply_solvency reads
    # ctx.config.credit_facility_rate alone) -- there is no year-over-year
    # rate PATH for 'variable' to switch on yet.
    DEAD_ALLOWLIST["liabilities[kind=line_of_credit].rate_type"] = ("#689",
        "mapped to property.credit_facility_rate_type for round-trip completeness, but "
        "simulation_rules.apply_solvency prices the facility off credit_facility_rate alone, "
        "for every year -- same finding as liabilities[kind=heloc].rate_type.")
    # .balance.amount -- the DRAWN opening balance has no legacy home, same
    # reasoning as liabilities[kind=heloc].balance.amount (#577): a draw is
    # a simulation decision (only the #679 waterfall draws it, in a
    # shortfall year), never a fact read off this field.
    DEAD_ALLOWLIST["liabilities[kind=line_of_credit].balance.amount"] = ("#689",
        "the DRAWN opening balance has no legacy home: only the #679 waterfall "
        "(simulation_rules.apply_solvency) ever draws this facility, in a shortfall year -- "
        "never a fact read off this field. Same finding as liabilities[kind=heloc].balance.amount "
        "(#577).")

for _kind in _EXAMPLE_LIABILITY_KINDS:
    if _kind in ("mortgage", "heloc", "line_of_credit"):
        continue
    # issue #763: the closed-end consumer kinds (car_loan, student_loan,
    # personal_loan) are now WIRED into the engine's consumer_loans path,
    # and intergenerational_loan is REFUSED loudly (#703) -- so none of them
    # is "parsed and dropped" anymore. Skip them here so this loop only
    # classifies a kind that is GENUINELY still dropped (a new kind added to
    # the enum without a mapping would land here, and the reachability
    # detector's liability gate would catch it too). The car_loan's wired
    # leaves are classified in the dedicated block below; student_loan/
    # personal_loan are not in the shipped example, so they have no leaves
    # here to classify (the detector's liability gate covers them via its
    # own per-kind fixtures).
    if _kind in ("car_loan", "student_loan", "personal_loan", "intergenerational_loan"):
        continue
    # Every REMAINING kind has NO legacy home at all -- confirmed by
    # test_contract_reachability.py's measured sweep (this kind's leaves are
    # DROPPED at the adapter). _find_liability is never even called with
    # this kind, so nothing about it -- including the fields
    # mortgage/heloc/line_of_credit DO get read for (collateral, balance,
    # rate) -- is read at all.
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].collateral"] = ("#603", "_find_liability is never "
        f"called with kind={_kind!r} -- this liability kind has no legacy home at all "
        f"(measured DROPPED), so even the fields mortgage/heloc DO get read for "
        f"(collateral) are simply never reached for this kind.")
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].balance.amount"] = ("#603",
        f"see liabilities[kind={_kind}].collateral above -- no legacy home at all.")
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].rate"] = ("#603",
        f"see liabilities[kind={_kind}].collateral above -- no legacy home at all.")
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].rate_type"] = ("#603",
        f"see liabilities[kind={_kind}].collateral above -- no legacy home at all.")
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].amortization.years"] = ("#603",
        f"see liabilities[kind={_kind}].collateral above -- no legacy home at all.")
    DEAD_ALLOWLIST[f"liabilities[kind={_kind}].amortization.payment_monthly"] = ("#603",
        f"see liabilities[kind={_kind}].collateral above -- no legacy home at all.")

# issue #763: car_loan is in the shipped example and is now WIRED -- its
# balance/rate/payment reach the engine's consumer_loans path (measured
# REACHING by test_contract_reachability.py). The four numeric leaves are
# CONSUMED; collateral (consumer loans are modeled unsecured; a non-null
# collateral is refused at load) and rate_type (no year-over-year consumer-
# loan rate PATH for 'variable' to switch on, same finding as heloc.rate_type)
# remain parsed-but-not-consumed.
if "car_loan" in _EXAMPLE_LIABILITY_KINDS:
    CONSUMED["liabilities[kind=car_loan].balance.amount"] = (
        "simulation_state.py", "loan['balance']")
    CONSUMED["liabilities[kind=car_loan].rate"] = (
        "simulation_rules.py", "loan['rate']")
    CONSUMED["liabilities[kind=car_loan].amortization.payment_monthly"] = (
        "simulation_rules.py", "loan['payment_monthly']")
    CONSUMED["liabilities[kind=car_loan].amortization.years"] = (
        "simulation_rules.py", "loan['amortization_years']")
    DEAD_ALLOWLIST["liabilities[kind=car_loan].collateral"] = ("#763",
        "consumer loans are modeled as UNSECURED -- a non-null collateral is "
        "REFUSED at load (input_contract.py, #763), so the null example value "
        "reaches no decision. A secured consumer loan against real estate would "
        "belong in the property's registered charge (#664/#689), not here.")
    DEAD_ALLOWLIST["liabilities[kind=car_loan].rate_type"] = ("#763",
        "the engine prices a closed-end consumer loan off its single declared "
        "rate (simulation_rules.apply_consumer_loans reads loan['rate'] alone) -- "
        "there is no year-over-year consumer-loan rate PATH for 'variable' vs "
        "'fixed' to switch on, same finding as liabilities[kind=heloc].rate_type.")


# ── provenance (#660, epic #659 Track 1) ──────────────────────────────────
#
# The sidecar is keyed by RFC 6901 JSON Pointer, so this guard's dict-walker
# (which has no notion of "arbitrary instance-specific key") produces one
# leaf PER (pointer, field) pair from schema/example.json's fabricated
# provenance block, not a generic "provenance.<field>" shape. Every one of
# them is genuinely read: provenance.load_provenance() validates every field
# of every entry against the document it annotates (a pointer that doesn't
# resolve, a measured entry missing source/as_of, an assumed/stated entry
# missing plausible_range/domain, or a range/domain that doesn't bracket/
# contain the leaf's live value are all rejected there -- see
# tests/test_provenance.py's ValidationTest), and Provenance.uncertain_
# leaves()/build_report() read confidence/plausible_range/domain/
# resolved_by to produce epic #659 Track 2's ranking and the --provenance
# CLI report. None of this reaches SimulationConfig's internal shape --
# the provenance sidecar is a parallel structure alongside the mapped
# document, deliberately outside to_internal_config's scope (#660's own
# module docstring) -- so every citation below points at provenance.py,
# never at input_contract.py.
for _pointer in (
    "/accounts/0/balance/amount",
    "/people/0/room/tfsa/contribution_room",
    "/accounts/6/acb",
    "/estate/default_spousal_rollover",
    "/people/0/legal_name",
):
    CONSUMED[f"provenance.{_pointer}.confidence"] = ("provenance.py", 'entry["confidence"]')

CONSUMED["provenance./accounts/0/balance/amount.source"] = ("provenance.py", 'entry.get("source")')
CONSUMED["provenance./accounts/0/balance/amount.as_of"] = ("provenance.py", 'entry.get("as_of")')
CONSUMED["provenance./people/0/room/tfsa/contribution_room.plausible_range"] = (
    "provenance.py", '"plausible_range" in entry')
CONSUMED["provenance./accounts/6/acb.resolved_by"] = ("provenance.py", 'entry.get("resolved_by")')
CONSUMED["provenance./estate/default_spousal_rollover.domain"] = ("provenance.py", '"domain" in entry')
CONSUMED["provenance./estate/default_spousal_rollover.resolved_by"] = ("provenance.py", 'entry.get("resolved_by")')


# ── Guard 1: the mechanical checks ───────────────────────────────────────

class SchemaCoverageTest(unittest.TestCase):

    def test_every_leaf_is_classified(self):
        """Every schema leaf is either consumed or explicitly allowlisted."""
        classified = set(DEAD_ALLOWLIST) | set(CONSUMED)
        unclassified = ALL_LEAVES - classified
        self.assertFalse(
            unclassified,
            "New schema leaf(s) with no coverage classification. Add each "
            "to DEAD_ALLOWLIST (leaf -> (issue, reason)) if it is parsed "
            "but never reaches a decision, or to CONSUMED (leaf -> (file, "
            "keyword)) citing the production line where it does, in "
            f"tests/test_schema_coverage.py: {sorted(unclassified)}",
        )

    def test_allowlist_entries_are_real_leaves(self):
        """DEAD_ALLOWLIST cannot silently outlive the leaf it describes."""
        stale = set(DEAD_ALLOWLIST) - ALL_LEAVES
        self.assertFalse(
            stale,
            f"DEAD_ALLOWLIST entries with no matching schema leaf (remove "
            f"them -- the key was deleted or renamed): {sorted(stale)}",
        )

    def test_consumed_entries_are_real_leaves(self):
        """Same hygiene check for the CONSUMED registry."""
        stale = set(CONSUMED) - ALL_LEAVES
        self.assertFalse(
            stale,
            f"CONSUMED entries with no matching schema leaf (remove them): "
            f"{sorted(stale)}",
        )

    def test_no_leaf_is_both_dead_and_consumed(self):
        overlap = set(DEAD_ALLOWLIST) & set(CONSUMED)
        self.assertFalse(
            overlap,
            f"Leaf(s) classified as both dead and consumed -- pick one: {sorted(overlap)}",
        )

    def test_dead_allowlist_entries_have_issue_and_reason(self):
        """No bare/lazy allowlist entries: every one names an issue and a reason."""
        for leaf, value in DEAD_ALLOWLIST.items():
            self.assertEqual(
                len(value), 2,
                f"{leaf}: DEAD_ALLOWLIST value must be (issue, reason)",
            )
            issue, reason = value
            self.assertTrue(issue and issue.strip(), f"{leaf}: missing issue reference")
            self.assertTrue(
                reason and len(reason.strip()) >= 10,
                f"{leaf}: reason is missing or too short to be meaningful",
            )

    def test_consumed_citations_are_still_true(self):
        """Every CONSUMED citation's keyword must still appear in its file."""
        failures = []
        for leaf, (rel_path, keyword) in CONSUMED.items():
            path = REPO_ROOT / rel_path
            if not path.exists():
                failures.append(f"{leaf}: cited file {rel_path} does not exist")
                continue
            content = path.read_text()
            if keyword not in content:
                failures.append(
                    f"{leaf}: keyword {keyword!r} no longer found in {rel_path} "
                    f"-- re-verify consumption, update the citation, or move "
                    f"this leaf to DEAD_ALLOWLIST"
                )
        self.assertFalse(failures, "Stale CONSUMED citations:\n" + "\n".join(failures))

    def test_consumed_citations_avoid_the_pure_loader(self):
        """A citation inside SimulationConfig's raw dict->dataclass parser
        proves parsing, not consumption (DP#32) -- reject that shortcut
        mechanically. input_contract.py is DELIBERATELY not excluded here
        (see module docstring): its selection/mapping logic (choosing the
        primary/spouse couple, computing ages from dates, matching income
        overrides to people) is a real decision, not a copy."""
        excluded = {"simulation_config.py"}
        offenders = [
            leaf for leaf, (rel_path, _kw) in CONSUMED.items()
            if Path(rel_path).name in excluded
        ]
        self.assertFalse(
            offenders,
            f"CONSUMED citation points at the pure dict->dataclass loader "
            f"(parsing is not consuming -- find where the value reaches a "
            f"decision instead): {offenders}",
        )

    def test_schema_files_present(self):
        """Sanity: the walker actually found the real files."""
        self.assertTrue(UNIVERSAL_SCHEMA_PATH.exists())
        self.assertTrue(CANADA_SCHEMA_PATH.exists())
        self.assertTrue(EXAMPLE_PATH.exists())
        self.assertGreater(len(ALL_LEAVES), 0)

    def test_legacy_schema_files_are_gone(self):
        """DP#9 (epic #603 Phase 2b): the example-instance files that used
        to masquerade as a schema are deleted, not merely superseded."""
        self.assertFalse((REPO_ROOT / "input_schema.json").exists())
        self.assertFalse((REPO_ROOT / "countries" / "canada" / "input_schema.json").exists())


# ── Guard 2: unknown keys are rejected ───────────────────────────────────
#
# Two layers now (epic #603 Phase 2b): the REAL wire-format guard is
# tests/test_input_contract.py's RejectionTest (additionalProperties:false
# throughout, enforced by validate_contract at the one loading boundary).
# This layer is SimulationConfig.from_dict's own, narrower guard over the
# internal dict shape it still accepts directly (see that module's
# docstrings) -- carried over unchanged from Phase 1/2a.

class UnknownKeyRejectionTest(unittest.TestCase):

    def test_unknown_key_is_rejected(self):
        from simulation_config import SimulationConfig

        cfg = _minimal_valid_config()
        cfg["family"]["mebmers_typo_should_not_be_silently_ignored"] = [{"anything": 1}]

        with self.assertRaises((ValueError, KeyError, TypeError)):
            SimulationConfig.from_dict(cfg)


def _minimal_valid_config():
    return {
        "family": {
            "members": [
                {"role": "primary", "birth_year": 1980, "gross_income": 100000},
                {"role": "spouse", "birth_year": 1981, "gross_income": 80000},
            ],
            "children": [],
        },
        "property": {
            "house_value": 500000,
            "mortgage_balance": 300000,
            "mortgage_rate": 0.05,
        },
        "savings": {"rate": 0.2},
        "assumptions": {"start_year": 2026},
        "tax": {"province": "qc"},
    }


if __name__ == "__main__":
    unittest.main()
