#!/usr/bin/env python3
"""The input contract (issue #602, epic #603, Track C, Phase 2b).

This module is the ONLY place that knows about the dated/owned/entity
shaped input document described by #596-#601. It is now the SOLE wire
format the engine accepts -- there is no other schema a caller may hand to
``SimulationConfig.from_json`` (Phase 1's ``input_schema.json`` /
``countries/canada/input_schema.json`` example-instance files, and the
loose, unvalidated dict shape they described, are deleted -- DP#9). It has
three jobs:

1. ``compose_schema()`` -- deterministically merge the two schema files
   (``schema/input_schema.json`` universal + ``schema/countries/canada/
   input_schema.json`` overlay) into one draft-2020-12 JSON Schema object
   used for validation. The two-file split is a deliberate, explicit
   decision (kept over Atlas's single-file draft) because ~60 open
   jurisdiction issues and 3 in-flight country PRs depend on the seam
   existing (see the PR body). The merge rule is total and declarative --
   never an ``or``-fallback over instance DATA (that is exactly the DP#32
   failure class this whole epic exists to eliminate); it is a one-time,
   deterministic merge of two SCHEMA documents at import time.

2. ``validate_contract()`` -- validates a candidate document against the
   composed schema and raises a ``ContractValidationError`` (a ``ValueError``
   subclass) listing every violation, not just the first (a machine author
   gets a complete error report, not one-at-a-time whack-a-mole).

3. ``to_internal_config()`` -- the single, total, explicit mapping from a
   validated contract document onto ``SimulationConfig``'s internal dict
   representation (the shape ``SimulationConfig.from_dict`` builds the
   dataclass from). This is NOT a second wire format: nothing outside this
   module and ``SimulationConfig`` itself ever authors that internal shape
   from scratch as a document on disk -- it exists only as ``SimulationConfig``
   / ``ScenarioOverlay`` / ``apply_overlay``'s in-memory working
   representation for scenario generation (DP#18/DP#24), a mechanism
   entirely internal to the simulation/optimization layers (DP#25), not
   part of the input contract. ``load_and_map()`` below is the one function
   every loading boundary (``SimulationConfig.from_json`` and every CLI
   script's ``--input`` flag) calls to go from an on-disk contract document
   to that internal shape -- there is exactly one path in, mandatory, not
   an opt-in step a caller can bypass.

   KNOWN, DECLARED LIMITATION (narrowed by #698 / #643 Step 8, still real):
   the internal engine drives its full ADULT compute -- per-adult RRSP/TFSA/
   FHSA contribution+growth, spousal RRSP, benefit decumulation, per-adult tax
   -- for exactly ONE couple. ``SimState.initial`` seeds only primary+spouse,
   the ``YearWorkingState`` carries two RRSP/TFSA scalars, and
   ``simulate_year_pure``'s signature is primary/spouse marginal rate (the
   N-slot rule interface Step 5 deferred). This mapping therefore selects ONE
   couple (the pair named by ``decisions.horizon.person`` and their spouse, or
   the first ``spouse_of`` pair found). Steps 2-7 made the account stores
   (#700), tax loop (#701) and estate (#705) iterate members, and epic #841 +
   Step 6 made every DEPENDENT child a first-class savings/tax subject -- so
   #698 relaxed the boundary to admit additional dependent GENERATIONS (of any
   depth: a grandchild reaches ``_map_child`` exactly like a direct child).
   What is STILL refused loudly (not silently truncated) is an additional
   ADULT -- a second couple / a benefit-drawing grandparent -- because holding
   them needs the N-adult compute rewrite (#706 / Step 9), not just a wider
   boundary. A 4-generation, multi-couple document (``schema/example.json``,
   six adults) is thus still refused; the couple-plus-children golden mapping
   is byte-identical.

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

import json
import logging
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = REPO_ROOT / "schema"
UNIVERSAL_SCHEMA_PATH = SCHEMA_DIR / "input_schema.json"
#: Key in ``UNIVERSAL_SCHEMA_PATH`` listing the ``$defs`` fragment files that
#: complete the universal schema (paths relative to ``SCHEMA_DIR``). The list is
#: DATA in the schema file, not a constant in this module (DP#2/DP#14), and it
#: is REQUIRED: a root file that does not declare it raises ``KeyError`` at
#: import rather than composing a silently-truncated schema (DP#32).
UNIVERSAL_PARTS_KEY = "x-schema-parts"
CANADA_OVERLAY_SCHEMA_PATH = SCHEMA_DIR / "countries" / "canada" / "input_schema.json"
EXAMPLE_PATH = SCHEMA_DIR / "example.json"


class ContractValidationError(ValueError):
    """Raised by ``validate_contract`` -- carries every violation found."""


class ContractAdaptationError(ValueError):
    """Raised by ``to_internal_config`` when a validated document contains
    something this mapping honestly cannot represent yet (e.g. more than one
    couple's worth of generations -- #598, the engine's own structural limit,
    not a schema limit) -- loud refusal, never a silent partial mapping
    (DP#32)."""


# ── 1. Schema composition ────────────────────────────────────────────────

def _merge_fragment(base: Dict, overlay: Dict) -> Dict:
    """Merge one overlay object-schema FRAGMENT into one base object-schema
    fragment (both already dicts, e.g. two ``$defs`` entries of the same
    name, or the root schema's top-level fragment). Total and deterministic:

    - ``properties``: shallow dict union; a key present in both is REPLACED
      by the overlay's definition (the overlay is refining/replacing a
      jurisdiction-agnostic placeholder with the real jurisdiction shape --
      #602's hard decision, not a fallback over instance data).
    - ``required``: base's list followed by overlay's additions, deduplicated,
      base order preserved (a union of what's required, never a subtraction).
    - ``allOf``: base's list followed by overlay's list, concatenated. allOf
      is already an AND of its members, so concatenation is the union of
      constraints -- not a choice between them.
    - every other key the overlay defines (``enum``, ``const``, ``type``,
      ``minItems``, ``description``, ``additionalProperties``, ...) REPLACES
      the base's value for that key, or is added if base didn't have it.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key == "properties":
            merged["properties"] = {**merged.get("properties", {}), **value}
        elif key == "required":
            existing = list(merged.get("required", []))
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged["required"] = existing
        elif key == "allOf":
            merged["allOf"] = list(merged.get("allOf", [])) + list(value)
        else:
            merged[key] = value
    return merged


def load_universal_schema() -> Dict:
    """Assemble the universal (jurisdiction-agnostic) schema from its files.

    ``schema/input_schema.json`` holds the document spine -- metadata, the
    root ``required``/``allOf`` and the top-level ``properties`` -- and names
    its ``$defs`` fragments in ``x-schema-parts``. Each fragment is folded in
    with ``compose_schema`` itself, i.e. the SAME total, declarative merge the
    Canada overlay already goes through (DP#8/DP#9: one composition mechanism,
    not two). A fragment carries only ``$defs`` today, so the fold is a
    ``$defs`` union; the root-property refinement rule applies to it unchanged,
    which means a fragment can never invent a root key.

    The split is presentation, not semantics: the object returned here is the
    object the single 1,368-line file used to be.
    """
    root = json.loads(UNIVERSAL_SCHEMA_PATH.read_text())
    part_paths = root.pop(UNIVERSAL_PARTS_KEY)
    universal = root
    for rel in part_paths:
        universal = compose_schema(universal, json.loads((SCHEMA_DIR / rel).read_text()))
    return universal


def compose_schema(universal: Optional[Dict] = None, overlay: Optional[Dict] = None) -> Dict:
    """Merge the Canada overlay into the universal target schema.

    Root-level: every key in ``overlay['properties']`` must refine a key
    already present in ``universal['properties']`` (the overlay REFINES an
    existing universal field; it may never invent a new root key -- new root
    keys belong in the universal file, by construction of DP#14's
    universal/jurisdiction split). ``$defs``: overlay entries merge into a
    same-named base entry via ``_merge_fragment``, or are added verbatim if
    the base has no entry of that name (a genuinely jurisdiction-only shape,
    e.g. ``lira``/``lsif``/``fhsa``/``resp``/``person_room``).
    """
    if universal is None:
        universal = load_universal_schema()
    if overlay is None:
        overlay = json.loads(CANADA_OVERLAY_SCHEMA_PATH.read_text())

    composed = dict(universal)

    overlay_root_props = overlay.get("properties", {})
    unknown_root_keys = set(overlay_root_props) - set(universal.get("properties", {}))
    if unknown_root_keys:
        raise ContractValidationError(
            f"Canada overlay declares root propert{'y' if len(unknown_root_keys) == 1 else 'ies'} "
            f"not present in the universal schema (overlay may only refine an "
            f"existing key): {sorted(unknown_root_keys)}"
        )
    merged_props = dict(universal.get("properties", {}))
    for key, frag in overlay_root_props.items():
        merged_props[key] = _merge_fragment(merged_props[key], frag)
    composed["properties"] = merged_props

    merged_defs = dict(universal.get("$defs", {}))
    for name, frag in overlay.get("$defs", {}).items():
        if name in merged_defs:
            merged_defs[name] = _merge_fragment(merged_defs[name], frag)
        else:
            merged_defs[name] = frag
    composed["$defs"] = merged_defs

    # The overlay is a jurisdiction extension, not an independent schema --
    # its own $schema/$id/title are metadata about the fragment, not the
    # composed whole. Drop anything besides properties/$defs it might carry.
    composed.pop("$comment", None)
    return composed


_COMPOSED_SCHEMA: Optional[Dict] = None
_VALIDATOR: Optional["jsonschema.protocols.Validator"] = None


def get_validator():
    """Lazily build (and cache) the composed-schema Draft202012Validator."""
    global _COMPOSED_SCHEMA, _VALIDATOR
    if _VALIDATOR is None:
        _COMPOSED_SCHEMA = compose_schema()
        jsonschema.Draft202012Validator.check_schema(_COMPOSED_SCHEMA)
        _VALIDATOR = jsonschema.Draft202012Validator(_COMPOSED_SCHEMA)
    return _VALIDATOR


def validate_contract(document: Dict) -> None:
    """Validate ``document`` against the composed target schema.

    Raises ``ContractValidationError`` listing every violation (not just the
    first) when invalid. Returns None (no exception) when valid.
    """
    validator = get_validator()
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        formatted = "\n".join(
            f"  - {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in errors
        )
        raise ContractValidationError(
            f"Input contract failed validation ({len(errors)} error"
            f"{'s' if len(errors) != 1 else ''}):\n{formatted}"
        )


# ── 2. Mapping: contract document -> SimulationConfig's internal dict shape ──

def _people_by_id(doc: Dict) -> Dict[str, Dict]:
    return {p["id"]: p for p in doc["people"]}


def _find_primary_and_spouse(doc: Dict) -> (str, Optional[str]):
    """Pick ONE couple to drive the legacy engine (documented Phase 1
    limitation -- see module docstring). Preference order: the person named
    by ``decisions.horizon.person`` (and their spouse, if any); else the
    first ``spouse_of`` pair found; else the first person alone."""
    people = _people_by_id(doc)
    horizon_person = doc["decisions"]["horizon"]["person"]  # schema-required; caller has already validated
    if horizon_person and horizon_person in people:
        primary_id = horizon_person
    else:
        primary_id = None
        for pid, p in people.items():
            if any(r["type"] == "spouse_of" for r in p.get("relationships", [])):
                primary_id = pid
                break
        if primary_id is None:
            primary_id = next(iter(people))

    spouse_id = None
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


def _sale_calendar_year(sale: Dict) -> int:
    """The calendar year a property's ``sale`` fires (issue #956 bite C).

    Reads the sweepable numeric ``year`` leaf when present, and derives it from
    ``date`` otherwise (``int(date[:4])``). An explicit ``is not None`` test --
    never ``sale.get("year") or int(...)`` (DP#32: a year of 0 is a real value
    the schema permits, and ``or`` would make it unrepresentable). Exactly one
    of ``year``/``date`` is guaranteed present by the schema's ``property_sale``
    oneOf. Mirrors the spelling `_map_property_sale` / `_map_principal_sale`
    already use to carry the sale year onto the config.
    """
    return (sale["year"] if sale.get("year") is not None
            else int(sale["date"][:4]))


def _property_sold_by_terminal(prop: Dict, terminal_year: Optional[int]) -> bool:
    """Issue #964: is this property SOLD on/before the terminal (death) year?

    A property whose ``sale`` fires on/before the terminal year is NOT owned at
    death -- the disposition rule already converted it to portfolio cash (the
    reinvested proceeds), so the estate must NOT value it again at its death-year
    deemed disposition (that is the double-count #964 is about). A ``sale.year``
    beyond the terminal year never fires inside the projection -> the property IS
    still owned at death -> keep it in the estate. ``sale`` absent -> held to the
    horizon -> keep it. ``terminal_year`` None (the horizon does not date
    against the primary -- an unreachable estate path in practice, but defended
    here) -> conservatively keep the property (do not drop a value on an unknown
    terminal year -- DP#32: absence must not silently zero a real asset).
    """
    sale = prop.get("sale")
    if sale is None or terminal_year is None:
        return False
    return _sale_calendar_year(sale) <= terminal_year


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


# ── Account-kind coverage (issue #647): every one of the schema's twelve
# ``account_kind`` values must either reach the engine or refuse loudly --
# no silent drop may remain expressible (DP#32).
#
# Three buckets:
#  - _HOUSEHOLD_AGGREGATED_KINDS: kinds with their own dedicated,
#    multi-account-aware aggregation elsewhere in to_internal_config
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
    own dedicated aggregation in ``to_internal_config`` and are skipped
    here, not silently re-mapped by a second path.
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
    # CONTRACT BOUNDARY in to_internal_config (where a person is actually
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


def _find_property(doc: Dict, kind: str) -> Optional[Dict]:
    for prop in doc.get("properties", []):
        if prop["kind"] == kind:
            return prop
    return None


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
      - ``cash_back_min_house_amount``: this tranche's declared cash-back
        CONDITION -- the minimum house tranche amount the origination inflow
        is credited at (issue #1075 optimizer half); ABSENT when the
        cash_back block declares none, meaning the credit is unconditional
        (the pre-#1075 behaviour, DP#13: absence is not an opinion, and the
        key is never fabricated).

    ``id``/``collateral`` are carried for the rate_paths reconciliation
    (#685) and the charge-limit check (#664) respectively, which name the
    facility the way the raw liability used to.
    """
    balance = tranche["balance"]["amount"]
    cash_back = tranche.get("cash_back")
    normalized = {
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
    # DP#32: presence-based -- a cash_back block that declares a
    # min_house_amount carries it; one that does not carries NO key, so
    # "unconditional credit" and "conditional on $0" stay distinguishable.
    if cash_back is not None and cash_back.get("min_house_amount") is not None:
        normalized["cash_back_min_house_amount"] = cash_back["min_house_amount"]
    return normalized


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
    # Issue #1075 (optimizer half): the summed origination inflow is ONE
    # credit, so its condition is the STRICTEST declared one -- the whole
    # credit is withheld unless the swept house tranche clears every
    # declared threshold. Presence-based (DP#32): no declaration, no key.
    # (Read the RAW cash_back blocks -- ``matches`` are document
    # liabilities, not the normalized facilities; the base facility built
    # from ``first`` already carries the first tranche's condition, and
    # this max over ALL of them overwrites it with the true strictest.)
    declared_house_conditions = [
        m["cash_back"]["min_house_amount"] for m in matches
        if m.get("cash_back") is not None
        and m["cash_back"].get("min_house_amount") is not None]
    if declared_house_conditions:
        facility["cash_back_min_house_amount"] = max(declared_house_conditions)
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


def _reconcile_spending_figures(
        doc: Dict, living_costs: Optional[float],
        spending_target: Optional[float]) -> List[Dict]:
    """Flag when the contract's two spending figures disagree materially (#766).

    ``household_budget.annual_living_costs`` is the household's MEASURED (or
    derived) working-phase spend. ``assumptions.retirement.spending_target`` is
    the retirement-phase spend the decumulation drawdown sizes itself to. They
    are not the same quantity -- one is working-life (and excludes debt
    payments), one is retirement (net of tax) -- so they need not be equal: a
    retirement target can legitimately sit above working-life spend (more
    travel) or below it (mortgage paid, children independent).

    But a retirement target that sits FAR from measured working-life spend, with
    no reconciliation, is exactly the #766 defect: a GUESSED retirement figure
    silently outranks a MEASURED living-cost figure, and the decumulation
    shortfall it produces is an artifact of the guess, not of the household's
    finances. This surfaces the disagreement -- both values, the ratio, and each
    leaf's provenance confidence when the sidecar is present -- so every output
    surface that prints a decumulation number also prints the fact that the two
    spending figures the household declared do not agree.

    Returns one record when both figures are present and their ratio falls
    outside [0.75, 1.25] (a +/-25% band -- a heuristic that catches a MATERIAL
    gap without flagging benign small differences). Empty list otherwise.

    Pure (DP#3): no logging, no mutation. ``model_fidelity.spending_figure_conflicts()``
    reads the records back off the internal config so every report surface can
    name the same two figures the load-time warning did.
    """
    if living_costs is None or spending_target is None:
        return []
    if living_costs <= 0:
        return []
    ratio = spending_target / living_costs
    _BAND_LO, _BAND_HI = 0.75, 1.25
    if _BAND_LO <= ratio <= _BAND_HI:
        return []
    provenance = doc.get("provenance")
    provenance = {} if provenance is None else provenance

    def _conf(pointer: str) -> Optional[str]:
        entry = provenance.get(pointer)
        return entry.get("confidence") if isinstance(entry, dict) else None

    return [{
        "living_costs": living_costs,
        "spending_target": spending_target,
        "ratio": ratio,
        # The decumulation sizes to the retirement target, so a guessed target
        # is what biases the shortfall -- 'winner' names what the run uses.
        "winner": "spending_target",
        "living_costs_confidence": _conf("/household_budget/annual_living_costs"),
        "spending_target_confidence": _conf("/assumptions/retirement/spending_target"),
    }]


# ── The estate namespace (epic #603 Track C Phase 2c, issue #600) ────────
#
# Five things the engine used to SILENTLY ASSUME -- every one of them
# resolving in the favourable direction (measured by agent Inky in PR #616) --
# become real, declared inputs here. This is the mapping from the contract's
# `estate` namespace + the per-account/per-property designations onto the
# internal `estate` block that `objective.plan_from_config` turns into a
# `countries.canada.estate.EstatePlan`.

_REGISTERED_KINDS = frozenset({"rrsp", "spousal_rrsp", "rrif", "lira", "lif",
                               "dcpp", "dbpp", "lsif"})


def _owner_shares(owner) -> Dict[str, float]:
    """{person_id: fraction} for an account/property/liability owner_ref --
    a bare person id, or a {'joint': [{person, pct}, ...]} split (#601)."""
    if isinstance(owner, dict):
        return {j["person"]: j["pct"] for j in owner["joint"]}
    return {owner: 1.0}


def _weighted_rolled_fraction(doc: Dict, person_id: Optional[str],
                              kinds, overrides: Dict[str, bool],
                              default_rollover: bool) -> float:
    """The balance-weighted fraction of ``person_id``'s accounts of ``kinds``
    that actually roll to the surviving spouse.

    A single household boolean CANNOT express the shipped example, which
    declares ``default_spousal_rollover: true`` plus a per-account override
    declining the rollover on the spousal RRSP. Silently dropping that override
    would be precisely the #593 "parsed and dropped" defect this epic exists to
    end -- so it is folded in here, by balance, and the estate model consumes a
    FRACTION rather than a flag (see EstatePlan's docstring).

    Returns 0.0 when the person holds nothing of these kinds (nothing to roll --
    a real zero, not a defaulted one).
    """
    if person_id is None:
        return 0.0
    total = 0.0
    rolled = 0.0
    for acc in doc.get("accounts", []):
        if acc["kind"] not in kinds:
            continue
        share = _owner_shares(acc["owner"]).get(person_id, 0.0)
        if share <= 0:
            continue
        amount = acc["balance"]["amount"] * share
        total += amount
        # DP#32: `in` (explicit presence), never `or`/truthiness -- an override
        # declaring `false` must not read as "no override".
        rolls = overrides[acc["id"]] if acc["id"] in overrides else default_rollover
        if rolls:
            rolled += amount
    if total <= 0:
        return 0.0
    return rolled / total


def _family_pre_designations(doc: Dict, couple: List[str],
                            as_of_year: int) -> Dict[str, frozenset]:
    """The family-level PRE designation map across ALL the couple's real
    property (issue #969; ITA s.40(2)(b)): ``{property_id: designated_years}``
    for every couple-owned property, capped at ``as_of_year`` (a ``to: None``
    period runs through ``as_of_year``). The principal residence IS included --
    the exemption is one property per *family unit* per year, so the family
    window spans every property the couple could designate, the principal
    among them.

    This is the ONE spelling of the family's designations (DP#9): the estate
    path (``_map_pre_property_gains``) and the voluntary-sale path both read
    it, so a mid-horizon sale prices its gain against the SAME family window
    the deemed disposition at death does -- not the sold property's own span
    in isolation (the single-property approximation that over-shelters a
    second property's gain, #969). Reuses ``designated_years`` from
    ``countries.canada.pre_designation`` (DP#10/#25: the Canada program area
    owns the arithmetic; this jurisdiction-agnostic mapper imports it lazily).
    """
    from countries.canada.pre_designation import designated_years

    designations: Dict[str, frozenset] = {}
    for prop in doc.get("properties", []):
        shares = _owner_shares(prop["owner"])
        couple_share = sum(v for k, v in shares.items() if k in couple)
        if couple_share > 0:
            designations[prop["id"]] = frozenset(designated_years(
                prop.get("designated_principal_residence_years", []),
                as_of_year))
    return designations


def _check_pre_family_year_conflict(designations: Dict[str, frozenset]) -> None:
    """Reject a document that designates the same family-year for two
    properties (ITA s.40(2)(b): the exemption is one property per family unit
    per year). Raised loudly at the ONE contract-loading boundary (DP#32) --
    a document that double-claims a year is invalid input, not a silent
    pick-one -- so every caller of ``to_internal_config`` gets the refusal
    regardless of whether a sale or the estate ever prices a gain. Reuses
    ``family_year_conflict`` (DP#9: one spelling of the conflict detection).
    """
    from countries.canada.pre_designation import family_year_conflict

    conflict = family_year_conflict(designations)
    if conflict is not None:
        year, id_a, id_b = conflict
        raise ContractAdaptationError(
            f"Properties {id_a!r} and {id_b!r} both designate {year} as a "
            f"principal-residence year, but the exemption is one property per "
            f"family unit per year (ITA s.40(2)(b)). Allocate {year} to exactly "
            f"one of them -- a document cannot claim the exemption twice."
        )


def _family_pre_window(doc: Dict, couple: List[str],
                       as_of_year: int) -> Optional[int]:
    """The family's shared PRE designation horizon (issue #969), in whole
    years, or ``None`` when there is no family contest.

    ``None`` (the byte-identical legacy sentinel) when the couple owns fewer
    than two properties OR no property declares any designation year: a
    single property's own span IS the family window, and a family that
    designates nothing has no exemption to apportion, so a sale in those
    households falls back to the property's own window (unchanged, DP#32).
    Otherwise returns ``family_window_years(designations)`` -- the span from
    the earliest to the latest designated year across ALL the couple's
    properties (inclusive) -- the denominator the estate path and a voluntary
    sale both price a property's taxable fraction against. Reuses
    ``family_window_years`` (DP#9: one spelling of the family window).
    """
    from countries.canada.pre_designation import family_window_years

    designations = _family_pre_designations(doc, couple, as_of_year)
    _check_pre_family_year_conflict(designations)
    if len(designations) < 2 or not any(designations.values()):
        return None
    return family_window_years(designations)


def _map_pre_property_gains(doc: Dict, principal: Optional[Dict],
                            principal_acb: Optional[float], couple: List[str],
                            primary_id: str) -> Optional[tuple]:
    """Per-property, per-year principal-residence exemption for the couple's real
    property (issue #695, epic #690 bite 4; ITA s.40(2)(b)).

    Returns ``None`` -- the byte-identical legacy path -- unless the couple owns
    **two or more** properties AND at least one declares designation years: there
    is no exemption to *contest* with a single property, or with no designation
    declared anywhere, so those documents keep exactly the prior behaviour (the
    principal exempt iff designated, every other property fully taxed).

    When it engages, it returns the couple's real property as a tuple of
    ``{id, fmv, acb, taxable_fraction, is_principal}`` dicts (each amount the
    couple's SHARE of the property), where ``taxable_fraction`` is the part of the
    accrued gain the exemption does NOT shelter, computed by
    ``countries.canada.pre_designation`` from the designated year ranges. The
    Canada tax arithmetic is imported lazily (DP#25: this jurisdiction-agnostic
    mapper does not depend on the Canada package at import time).

    Raises ``ContractAdaptationError`` for two invalid documents: a family-year
    designated by two properties at once (the exemption is one-per-family-per-year,
    not a silent pick-one), and a property whose gain stays partly taxable but
    whose ``acb`` is null (an unknown cost base cannot be defaulted to 0 -- DP#32).
    """
    from countries.canada.pre_designation import (
        family_window_years, taxable_gain_fraction)

    as_of_year = int(doc["as_of"][:4])
    principal_id = principal["id"] if principal is not None else None

    owned = []  # every couple-owned real property, with the couple's share
    for prop in doc.get("properties", []):
        shares = _owner_shares(prop["owner"])
        couple_share = sum(v for k, v in shares.items() if k in couple)
        if couple_share > 0:
            owned.append((prop, couple_share))

    # The family's designations + the one-per-family-per-year conflict check
    # are the ONE spelling shared with the voluntary-sale path (issue #969,
    # DP#9): ``_family_pre_designations`` builds the map, `_check_pre_family_
    # year_conflict` rejects a double-claimed year loudly (DP#32).
    designations = _family_pre_designations(doc, couple, as_of_year)
    _check_pre_family_year_conflict(designations)

    # No contest -> legacy path (single property, or no designation at all).
    if len(owned) < 2 or not any(designations.values()):
        return None

    # Issue #964: a property SOLD on/before the terminal (death) year is not
    # owned at death -- its economics reach the estate ONLY through the
    # reinvested proceeds (already in the portfolio via the disposition rule).
    # Excluding it here keeps its death-value out of the estate's
    # `property_gains` (the double-count #964 is about). The terminal year is
    # the year the horizon person reaches `decisions.horizon.until_age` -- the
    # SAME terminal year `objective._estate_call_args` values the estate on
    # (`start_year + len(results) - 1`, which equals this for a horizon dated
    # against the primary). `primary_id` is the horizon person here (the caller
    # `_map_estate` resolves it as `decisions.horizon.person`).
    terminal_year = _horizon_end_year(doc, primary_id)

    window = family_window_years(designations)
    gains = []
    for prop, couple_share in owned:
        if _property_sold_by_terminal(prop, terminal_year):
            continue
        pid = prop["id"]
        is_principal = pid == principal_id
        fraction = taxable_gain_fraction(len(designations[pid]), window)
        acb = principal_acb if is_principal else prop.get("acb")
        if fraction > 0.0 and acb is None:
            raise ContractAdaptationError(
                f"Property {pid!r} keeps a taxable share of its gain after the "
                f"principal-residence exemption (taxable fraction {fraction:.4f}) "
                f"-- but its `acb` is null. An unknown cost base cannot be "
                f"defaulted to 0 (that would claim the entire value as gain): "
                f"state the acb, or designate it for enough years to exempt it."
            )
        # The principal residence carries its FULL value/acb (its value reaches
        # the estate through `house_equity`, and its member split is the shared
        # `property_primary_share`) -- exactly as the legacy `principal_residence_
        # fmv/acb` did. Every OTHER property is the couple's SHARE, as the legacy
        # aggregate `taxable_property_*` was. Keeping both conventions identical is
        # what makes the single-property households byte-identical (DP#9).
        share = 1.0 if is_principal else couple_share
        entry: Dict[str, Any] = {
            "id": pid,
            # Issue #963 (epic #956 bite F): the principal's `fmv` here is the
            # STATIC base (ownership-year) value -- the mapper does NOT compound
            # it because the terminal year is a simulation-result fact
            # (`start_year + len(results) - 1`), unknown at map time. The
            # principal's `appreciation_rate` is carried onto the entry so the
            # estate's deemed-disposition (`objective._estate_call_args`) can
            # compound this base to the terminal year itself (DP#9 -- one
            # spelling of the appreciated value, the same compounding
            # `simulation_rules._principal_value_for_year` uses). Absence-safe
            # (DP#32): an absent/None rate is not carried, so a household with
            # no appreciation (incl. the golden fixture) round-trips the static
            # `fmv` byte-identical. `acb` stays at cost (appreciation does not
            # change ACB -- DP#19). Carried only on the principal entry; a
            # non-principal property's appreciation is Bite A's concern, not
            # the estate's PRE allocation.
            "fmv": prop["value"]["amount"] * share,
            "acb": (0.0 if acb is None else acb) * share,
            "taxable_fraction": fraction,
            "is_principal": is_principal,
        }
        if is_principal:
            rate = principal.get("appreciation_rate") if principal else None
            if rate is not None:
                entry["appreciation_rate"] = rate
        gains.append(entry)
    return tuple(gains)


def _map_estate(doc: Dict, primary_id: str, spouse_id: Optional[str]) -> Dict[str, Any]:
    """Map the contract's estate FACTS + designations onto the internal estate
    block. Every value here was a silent assumption before Phase 2c (#600)."""
    estate = doc["estate"]  # schema-required
    default_rollover = estate["default_spousal_rollover"]
    overrides = {o["account"]: o["spousal_rollover"]
                 for o in estate["rollover_overrides"]}

    couple = [pid for pid in (primary_id, spouse_id) if pid is not None]

    # ── (1) Mortality: who dies first? Previously not modelled AT ALL -- there
    # was no way to say who dies when, so the estate could not know whose
    # terminal return the rolled assets even land on (#600).
    mortality = {m["person"]: m for m in doc["assumptions"]["mortality"]}
    primary_dies_first = _primary_dies_first(doc, mortality, primary_id, spouse_id)

    # ── (2) The rollover election, folded with per-account overrides, weighted
    # by balance. The FIRST-TO-DIE is the one whose assets can roll.
    first_id = primary_id if primary_dies_first else spouse_id
    registered_rolled = _weighted_rolled_fraction(
        doc, first_id, _REGISTERED_KINDS, overrides, default_rollover)
    non_reg_rolled = _weighted_rolled_fraction(
        doc, first_id, {"non_reg"}, overrides, default_rollover)

    # ── (3) TFSA successor holder vs beneficiary. Sheltered only if EVERY one
    # of the couple's TFSAs names a successor holder; a single TFSA left to a
    # plain beneficiary ends that shelter, and reporting the household as
    # "sheltered" because the OTHER one was would be the same favourable-
    # direction guess this issue is about.
    couple_tfsas = [a for a in doc.get("accounts", [])
                    if a["kind"] == "tfsa"
                    and set(_owner_shares(a["owner"])) & set(couple)]
    tfsa_successor = bool(couple_tfsas) and all(
        a.get("successor_holder") is not None for a in couple_tfsas)

    # ── (4) The non-registered ownership split -- the REAL one, from
    # accounts[kind=non_reg].owner (joint pct included), replacing the hardcoded
    # 50/50 guess over what is now the estate's single largest tax base (#613).
    non_reg_primary_share = _ownership_share(
        doc, "non_reg", primary_id, couple, default_share=0.5)

    # ── (5) Principal-residence designation + the property split. The exemption
    # (s.40(2)(b)) is claimed per property per year; a residence with NO
    # designation years is ordinary capital property and its gain IS taxed.
    principal = _find_property(doc, "principal")
    designated = bool(principal
                      and principal.get("designated_principal_residence_years"))
    # Issue #964: a property SOLD on/before the terminal (death) year is NOT
    # owned at death -- the disposition rule already converted it to portfolio
    # cash (reinvested proceeds), so the estate must not value it AGAIN at its
    # death-year deemed disposition (the double-count this issue is about). The
    # terminal year is the year the horizon person reaches
    # `decisions.horizon.until_age` -- the SAME terminal year the estate's
    # deemed-disposition (`objective._estate_call_args`) values on
    # (`start_year + len(results) - 1`, which equals this for a horizon dated
    # against the primary). `primary_id` is `decisions.horizon.person` (resolved
    # by the caller above). A sold principal zeros `principal_fmv`/`house_equity`
    # here AND in `objective._estate_call_args` (which independently re-derives
    # the home's value from `cfg['property']['house_value']`) -- both sides must
    # agree, or the estate values a home the household no longer owns.
    terminal_year = _horizon_end_year(doc, primary_id)
    principal_sold = (principal is not None
                      and _property_sold_by_terminal(principal, terminal_year))

    principal_fmv = (principal["value"]["amount"] if principal and not principal_sold
                     else 0.0)
    principal_acb = (principal.get("acb") if principal else None)
    if principal is not None and not designated and principal_acb is None:
        # DP#32: a $0 ACB is not "unknown" -- it is a 100%-gain claim. Refuse to
        # invent it. (A DESIGNATED residence needs no ACB: its gain is exempt.)
        raise ContractAdaptationError(
            f"Property {principal['id']!r} is a principal residence with NO "
            f"designated_principal_residence_years, so its accrued gain is "
            f"taxable at death (ITA s.40(2)(b) applies only to designated "
            f"years) -- but its `acb` is null. An unknown cost base cannot be "
            f"defaulted to 0: that would silently claim the ENTIRE value as an "
            f"accrued gain. State the acb, or designate the residence."
        )

    # Non-principal real property owned by the couple (a cottage, a rental):
    # ordinary capital property -- value in the estate, gain taxed.
    other_fmv = 0.0
    other_acb = 0.0
    other_primary_amount = 0.0
    for prop in doc.get("properties", []):
        if principal is not None and prop["id"] == principal["id"]:
            continue
        shares = _owner_shares(prop["owner"])
        couple_share = sum(v for k, v in shares.items() if k in couple)
        if couple_share <= 0:
            continue  # someone else's property (e.g. the grandparents' cottage)
        # Issue #964: a non-principal property SOLD on/before the terminal year
        # is not owned at death -- skip it (its proceeds are in the portfolio).
        if _property_sold_by_terminal(prop, terminal_year):
            continue
        if prop.get("acb") is None:
            raise ContractAdaptationError(
                f"Property {prop['id']!r} (kind={prop['kind']!r}) is not the "
                f"principal residence, so its accrued gain is taxable at death "
                f"-- but its `acb` is null. An unknown cost base cannot be "
                f"defaulted to 0 (that would claim the entire value as gain)."
            )
        other_fmv += prop["value"]["amount"] * couple_share
        other_acb += prop["acb"] * couple_share
        other_primary_amount += prop["value"]["amount"] * shares.get(primary_id, 0.0)

    # ── (5b) Per-year PRE allocation across the couple's properties (issue #695,
    # epic #690 bite 4). Until now the year ranges were parsed and never
    # compared: the principal was exempt iff it declared ANY year and every other
    # property was fully taxed, so a family that validly designated its cottage
    # for some years got NO exemption on it and full tax on the home. Here the
    # `from`/`to` ranges finally move the tax -- one property per family-year,
    # apportioned per ITA s.40(2)(b) -- and a document that double-claims a
    # family-year is rejected loudly (not silently pick-one'd).
    property_gains = _map_pre_property_gains(
        doc, principal, principal_acb, couple, primary_id)

    # The property ownership split spans the principal residence AND any others
    # -- it is a separate fact from the non-registered split (#595: two
    # unrelated facts must not be derived from one another).
    principal_primary_amount = (
        principal_fmv * _owner_shares(principal["owner"]).get(primary_id, 0.0)
        if principal else 0.0)
    property_total = principal_fmv + other_fmv
    property_primary_share = (
        (principal_primary_amount + other_primary_amount) / property_total
        if property_total > 0 else 0.5)

    # ── (6) Life insurance: a tax-free death benefit (ITA s.148(1)), absent
    # from the model entirely until now. Only policies INSURING a member of the
    # couple pay into THIS estate, and a TERM policy that has already lapsed by
    # the projection horizon pays nothing -- a term policy is not a permanent
    # one, and treating it as such would inflate the estate by its full face.
    horizon_date = _horizon_date(doc, primary_id)
    death_benefit = 0.0
    for pol in estate["life_insurance"]:
        if pol["insured"] not in couple:
            continue
        term_end = pol.get("term_end_date")
        if term_end is not None and horizon_date is not None and term_end < horizon_date:
            logger.info(
                "Life-insurance policy %r (term, face $%s) expires %s, before the "
                "projection horizon %s -- it pays no death benefit into the "
                "terminal estate and is excluded.",
                pol["id"], f"{pol['face_amount']:,.0f}", term_end, horizon_date)
            continue
        death_benefit += pol["face_amount"]

    # Issue #963 (epic #956 bite F): carry the principal's `appreciation_rate`
    # onto the estate block so the estate's deemed-disposition
    # (`objective._estate_call_args`) can compound the STATIC
    # `principal_residence_fmv` to the terminal calendar year itself. The
    # mapper carries the RATE only -- never the appreciated value -- because
    # the terminal year is a simulation-result fact
    # (`start_year + len(results) - 1`), unknown at map time (compounding here
    # would bake in a fixed horizon and lie for an overlay that moves it).
    # Absence-safe (DP#32): an absent/None rate is not carried, so a household
    # that declares no appreciation (incl. the golden fixture, whose legacy
    # `property` dict never carries this key) round-trips the static
    # `principal_residence_fmv` byte-identical -- the objective layer's
    # absence-test returns the static value and never reads a rate. A negative
    # rate is honored (a falling market is a real scenario a sell/keep sweep
    # must be robust to). Mirrors the rate `to_internal_config` already carries
    # onto `cfg['property']['appreciation_rate']`; carrying it here too makes
    # the estate block self-describing (the estate's property data carries its
    # own appreciation, not a pointer to another block) -- DP#9, one spelling
    # of the rate the estate consumes, read from the estate block.
    principal_appreciation_rate = (
        principal.get("appreciation_rate") if principal and not principal_sold
        else None)

    estate_block: Dict[str, Any] = {
        "spousal_rollover": default_rollover,
        "primary_dies_first": primary_dies_first,
        "registered_rolled_fraction": registered_rolled,
        "non_reg_rolled_fraction": non_reg_rolled,
        "tfsa_successor_holder": tfsa_successor,
        "non_reg_primary_share": non_reg_primary_share,
        "property_primary_share": property_primary_share,
        "life_insurance_death_benefit": death_benefit,
        "taxable_property_fmv": other_fmv,
        "taxable_property_acb": other_acb,
        "principal_residence_designated": designated,
        # The STATIC base (ownership-year) value; the objective layer
        # compounds it to the terminal year when a rate is carried below.
        "principal_residence_fmv": principal_fmv,
        "principal_residence_acb": 0.0 if principal_acb is None else principal_acb,
        "property_gains": property_gains,
    }
    if principal_appreciation_rate is not None:
        estate_block["principal_residence_appreciation_rate"] = (
            principal_appreciation_rate)
    return estate_block


def _map_principal_sale(principal: Optional[Dict], mortgage: Optional[Dict],
                        heloc: Optional[Dict], primary_id: Optional[str],
                        spouse_id: Optional[str],
                        family_pre_window: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Issue #956 bite E: map a declared SALE of the PRINCIPAL residence onto
    the config seam the principal's own rule reads.

    The principal residence is a `kind="principal"` property in
    `doc["properties"]`; the schema's `property_sale` block (Bite B) is reused
    verbatim -- a declared `sale` here means the household sells the principal
    in `sale.year` (or the calendar year of `sale.date`), the home + its
    mortgage + any HELOC/SM secured against it leave the balance sheet from
    the sale year, and the net proceeds (gross value less the discharged debt,
    selling costs, and the disposition tax) are invested into the portfolio
    (non-reg). Omitted/null (the golden household and every hold case) -> the
    principal is held to the horizon -> this returns None -> a strict no-op
    (DP#32): the golden invariant is unchanged by construction.

    Returns the same shape Bite B carries on a non-principal `sale` so the
    disposition rule reads one consistent contract:

      - `year`: the sale calendar year (the sweepable numeric `year` leaf
        Bite C added, else `int(date[:4])` -- an explicit `is not None` test,
        never `sale.get("year") or int(...)` (DP#32 forbids `x or DEFAULT`)).
      - `selling_costs`: the couple's share of the one-time disposition costs
        (realtor/notary/inspection); null `selling_costs` is a real $0, read
        explicitly (DP#32).
      - `owner_roles`: each taxed member's share of the property (Canada has
        no joint filing -> each spouse's gain bands against their own return),
        mirroring the non-principal sale's owner_roles spelling exactly.
      - `designated_principal_residence_years`: the PRE periods, carried raw
        so the rule apportionments the gain (ITA s.40(2)(b)). A principal
        designated for its whole ownership is FULLY PRE-exempt -> tax ~ 0.
      - `value_share`: the couple's share of the gross value (the principal's
        FULL value at the couple's ownership % -- the BASE ownership-year value
        the sale realizes; when the principal declares `appreciation_rate`
        (#963 bite F) the disposition rule compounds this base to the sale year,
        so a downsize/sell realizes the GROWN home, not the static figure).
      - `acb_share`: the couple's share of the adjusted cost base. A null
        `acb` means "no accrued gain yet" (bought at value) -> `value_share`
        (the disposition gain collapses to 0, DP#32: never 0.0 as a fallback).

    `secured_share` is NOT carried: the principal's mortgage AMORTIZES in the
    engine (the non-principal path's `secured_share` is a static snapshot of
    a mortgage the engine does not amortize -- it cannot amortize, the
    non-principal mortgage is not in the amortization schedule). The debt
    discharged at the principal's sale is the LIVE year-N balance the rule
    reads off `YearWorkingState` (`new_mortgage_balance` / `new_heloc_balance`
    / `new_sm_heloc`), not a config-time snapshot -- carrying a snapshot here
    would under-state the retired debt (the mortgage has amortized down) and
    break the conservation identity.
    """
    if principal is None:
        return None
    sale = principal.get("sale")
    if sale is None:
        return None
    shares = _owner_shares(principal["owner"])
    couple = [pid for pid in (primary_id, spouse_id) if pid is not None]
    couple_share = sum(frac for pid, frac in shares.items() if pid in couple)
    # A principal the couple does not own cannot be sold by them -- refuse
    # loudly (DP#32) rather than silently carrying a 0-share sale that would
    # inject no proceeds and discharge no debt (a no-op masquerading as a
    # disposition, the exact silent-zero failure this repo exists to prevent).
    if couple_share <= 0:
        raise ContractAdaptationError(
            f"Property {principal['id']!r} declares a sale but the couple owns "
            f"a 0 share of it -- the household cannot sell a home it does not "
            f"own. Declare the owner, or remove the sale (DP#32)."
        )
    # The sale calendar year: the sweepable numeric `year` leaf when present
    # (Bite C), else `int(date[:4])`. Explicit `is not None` -- never
    # `sale.get("year") or int(...)` (DP#32: a year of 0 is a real value the
    # schema permits; `or` would mask it). Exactly one of `year`/`date` is
    # guaranteed by the schema's `property_sale` oneOf.
    syear = (sale["year"] if sale.get("year") is not None
             else int(sale["date"][:4]))
    sc = sale.get("selling_costs")
    selling_costs = (sc if sc is not None else 0.0) * couple_share
    owner_roles = {
        role: shares.get(pid, 0.0)
        for role, pid in (("primary", primary_id), ("spouse", spouse_id))
        if pid is not None and shares.get(pid, 0.0) > 0
    }
    value_share = principal["value"]["amount"] * couple_share
    acb = principal.get("acb")
    acb_share = (acb if acb is not None else value_share) * couple_share
    sale_entry: Dict[str, Any] = {
        "year": syear,
        "selling_costs": selling_costs,
        "owner_roles": owner_roles,
        "designated_principal_residence_years":
            principal.get("designated_principal_residence_years", []),
        "value_share": value_share,
        "acb_share": acb_share,
    }
    # Issue #969: carry the family-level PRE window onto the sale so the
    # disposition rule prices the gain against the FAMILY denominator (ITA
    # s.40(2)(b)), not this property's own span in isolation. Carried ONLY
    # when a genuine family contest exists (>=2 properties with designations);
    # ``None`` (absent) is the byte-identical legacy sentinel -- a single-
    # property household falls back to its own window, unchanged (DP#32).
    if family_pre_window is not None:
        sale_entry["family_pre_window"] = family_pre_window
    # Issue #963 (epic #956 bite F): carry the principal's `appreciation_rate`
    # onto the sale so the disposition rule can price the APPRECIATED gross
    # value at the sale year (a downsize/sell realizes the GROWN home, not the
    # static `value_share`). `value_share` above is the base (ownership-year)
    # value; the rule compounds it by `(1 + rate) ** (syear - start_year)` and
    # uses the appreciated value as the sale's gross (the gain base and the
    # proceeds leg both use it), while `acb_share` stays at cost (appreciation
    # does not change ACB -- DP#19). Carried only when declared so a sale
    # with no rate realizes the static `value_share` byte-identical to bite E
    # (DP#32): the rule's absence-test reads `value_share` directly.
    appreciation_rate = principal.get("appreciation_rate")
    if appreciation_rate is not None:
        sale_entry["appreciation_rate"] = appreciation_rate
    return sale_entry


def _annual_amortization_schedule(principal: float, annual_rate: float,
                                 amortization_years: float,
                                 origination_year: int,
                                 projection_years: int) -> List[Dict[str, float]]:
    """Issue #967: a mid-horizon mortgage's ANNUAL amortization schedule,
    one entry per calendar year from ``origination_year`` to the payoff year
    (or the projection horizon, whichever comes first).

    A pure function (DP#3) of the originated principal, the declared rate,
    the amortization term, and the origination year -- it reads no simulation
    state, so the schedule the servicing rule amortizes against and the
    interest the rental deduction claims are ONE spelling (DP#9), never two
    computations that could drift. It reuses the standard annuity formula
    (``countries.canada.rate_model.monthly_payment`` -- the SAME function the
    year-0 principal mortgage's schedule is built from), so this does NOT
    write a new amortization engine; it builds a per-property annual slice
    from the existing one.

    Each entry: ``{year, opening_balance, interest, principal, payment,
    end_balance}``. ``interest`` is ``opening_balance * annual_rate`` (the
    annual interest charged on the outstanding balance -- the figure the
    rental deduction claims under s.20(1)(c)). ``principal`` is
    ``payment - interest``. In the payoff year the remaining balance is
    closed exactly (``payment = opening_balance + interest``) so no residual
    lingers past the term, mirroring ``apply_consumer_loans``'s final-year
    close. A 0% rate amortizes straight-line (principal / term), the same
    degenerate path ``monthly_payment`` takes. The schedule stops at the
    payoff year (``end_balance <= 0``); entries past it are NOT emitted, so a
    mortgage that pays off before the horizon contributes nothing after.
    """
    from countries.canada.rate_model import monthly_payment
    schedule: List[Dict[str, float]] = []
    if principal <= 0 or amortization_years <= 0:
        return schedule
    monthly = monthly_payment(principal, annual_rate, amortization_years)
    annual_payment = monthly * 12.0
    balance = principal
    # A mortgage cannot outlive its amortization term: count the payoff year
    # from the term (the same term-starts-at-origination convention
    # apply_consumer_loans uses), capped at the projection horizon so a long
    # term does not build entries past the run.
    term_years = int(amortization_years) if amortization_years == int(amortization_years) else None
    for i in range(projection_years):
        year = origination_year + i
        if balance <= 1e-9:
            break
        interest = balance * annual_rate
        # The final year of the declared term: close the loan exactly.
        if term_years is not None and i + 1 >= term_years:
            payment = balance + interest
        else:
            payment = min(annual_payment, balance + interest)
        principal_paid = payment - interest
        end_balance = max(0.0, balance - principal_paid)
        schedule.append({
            'year': year,
            'opening_balance': balance,
            'interest': interest,
            'principal': principal_paid,
            'payment': payment,
            'end_balance': end_balance,
        })
        balance = end_balance
    return schedule


def _map_owned_properties(doc: Dict, primary_id: str,
                          spouse_id: Optional[str],
                          projection_years: int = 0,
                          family_pre_window: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every NON-principal real property the couple owns, as a first-class
    ``{id, kind, net_equity}`` list for the ANNUAL balance sheet (issue #692,
    epic #690 bite 1).

    Before this seam the annual side (``prop_cfg`` -> ``house_value``) read
    exactly ONE ``kind="principal"`` residence; a declared cottage or rental
    the couple also owned was dropped from ``SimState`` and so from every
    annual metric (net worth / ``total_assets``), surfacing only at the
    terminal estate (``_map_estate`` already values + taxes it there). This
    carries each such property forward so the household's stated real estate
    is on the annual balance sheet, not silently truncated to the first match
    (DP#32 -- absence must be explicit, not a favourable understatement).

    The principal residence is EXCLUDED here on purpose: its value already
    reaches the annual side via ``prop_cfg`` (LTV / charge math) and is not
    what #692 reports dropped. Counting it here too would double it.

    ``net_equity`` is the couple's share of ``value - (mortgage/heloc secured
    against THIS property)``. It is a STATIC balance-sheet figure this bite:
    rental income (#693), CCA (#694), the per-year PRE allocation (#695), a
    mid-horizon purchase (#696) and STR (#697) are later bites that give the
    property dynamics -- deliberately not modelled here.
    """
    couple = [pid for pid in (primary_id, spouse_id) if pid is not None]
    principal = _find_property(doc, "principal")
    owned: List[Dict[str, Any]] = []
    for prop in doc.get("properties", []):
        if principal is not None and prop["id"] == principal["id"]:
            continue
        shares = _owner_shares(prop["owner"])
        couple_share = sum(frac for pid, frac in shares.items() if pid in couple)
        if couple_share <= 0:
            continue  # someone else's property (e.g. the grandparents' cottage)
        # A mortgage/HELOC whose collateral is THIS property reduces its
        # equity. Such a facility is not the principal residence's charge, so
        # it never reached mortgage_balance/heloc_balance (those are looked up
        # by the principal's id) -- netting it here is the only place this
        # property's financing is accounted for, so there is no double count.
        secured_liabs = [
            liab for liab in doc.get("liabilities", [])
            if liab["kind"] in ("mortgage", "heloc")
            and liab.get("collateral") == prop["id"]
        ]
        secured = sum(liab["balance"]["amount"] for liab in secured_liabs)
        # Issue #967: a MORTGAGE originated at a mid-horizon purchase
        # (purchase.financing) is a secured liability against THIS property
        # that does NOT exist at year 0 (it originates in the purchase year),
        # so it is not among the year-0 `secured_liabs` above. It reduces the
        # property's equity the SAME way a year-0 secured mortgage does --
        # the down payment (value - mortgage_amount) is what the household
        # funds, not the full value -- so its principal joins `secured` here
        # for the net_equity / secured_share computation. `financing` is read
        # ONLY off a declared `purchase` (the schema requires purchase to
        # carry financing), and only when present, so a property with no
        # financing round-trips byte-identical to #696 (DP#32). The principal
        # is taken at the couple's share, mirroring liability.balance above.
        purchase = prop.get("purchase")
        financing = (purchase.get("financing")
                     if purchase is not None else None)
        # Issue #1011: `funding_options` is the SWEEP form of `financing` --
        # the optimizer enumerates and ranks candidate funding methods for this
        # purchase rather than funding it one fixed way. The schema makes
        # `financing` and `funding_options` mutually exclusive (a purchase is
        # either funded one fixed way or swept, never both), so when
        # `funding_options` is declared `financing` is None. Read here ONLY to
        # carry it onto the annual-side entry for the discovery/exploration to
        # enumerate; it is NOT a financing input itself (the engine never
        # services a `funding_options` list -- it services the `financing`
        # block the overlay materializes per chosen option). Absent => no
        # funding sweep, byte-identical to #967/#696 (DP#32).
        funding_options = (purchase.get("funding_options")
                           if purchase is not None else None)
        financed_principal = 0.0
        if financing is not None:
            financed_principal = financing["mortgage_amount"]
            secured += financed_principal
        net_equity = (prop["value"]["amount"] - secured) * couple_share
        entry: Dict[str, Any] = {
            "id": prop["id"],
            "kind": prop["kind"],
            "net_equity": net_equity,
        }
        # Issue #956 bite A: a declared real annual appreciation rate lets the
        # property's GROSS value compound year over year (equity = appreciated
        # value - the static secured mortgage), so a purchase-timing sweep sees
        # the dominant driver of real-estate timing. Carry the couple's share of
        # gross value and secured mortgage SEPARATELY (net_equity alone cannot be
        # appreciated -- the mortgage portion must not grow). Emitted only when
        # appreciation_rate is declared, so a property with no rate round-trips
        # byte-identically to #692/#696 (DP#32). The rate is a sweepable numeric
        # leaf (sensitivity.sweeps) so the optimizer can range the assumption.
        #
        # Issue #956 bite B: a dated mid-horizon SALE (prop["sale"]) needs the
        # SAME couple-share gross value, secured mortgage, and adjusted cost
        # base to price its disposition gain (value_share - acb_share is the
        # gross gain; the secured_share is the debt retired at sale). The tax
        # layer built on top of this bite consumes all three. So the shares are
        # carried whenever appreciation_rate OR sale is declared; a property
        # with neither still round-trips byte-identically to #692/#696 (DP#32).
        #
        # `acb_share`: the couple's share of the adjusted cost base. The schema
        # declares `property.acb` as `money | null` (a bare number, not an
        # {amount} object -- read directly, mirroring `_map_pre_property_gains`
        # which does `prop["acb"] * share`). A null ACB is not unknown-to-zero;
        # it means "no accrued gain yet" -- a property bought at value has
        # ACB == value -- so the fallback is `value_share`, never 0.0 (DP#32:
        # zero is a value, not a fallback).
        appreciation_rate = prop.get("appreciation_rate")
        sale = prop.get("sale")
        # Issue #967: a financed purchase also needs the couple-share gross
        # value and secured mortgage carried SEPARATELY (net_equity alone
        # cannot be amortized -- the financed mortgage portion is a serviced
        # liability that amortizes down, so the equity = appreciated value -
        # the (declining) mortgage must read value_share / secured_share, not
        # a static net_equity). So the shares are carried when
        # appreciation_rate OR sale OR financing is declared; a property with
        # none of the three still round-trips byte-identical to #692/#696
        # (DP#32).
        #
        # Issue #1010: a carrying_costs block declaring a `fraction_of_value`
        # ALSO needs the couple-share gross value carried, because the fraction
        # is applied to the property's CURRENT (appreciating) gross value --
        # `net_equity` alone cannot be fractioned (it is net of the secured
        # mortgage, and property tax is assessed on gross value, not equity).
        # So the gate fires for a non-null `fraction_of_value` too. A
        # carrying_costs block with only a flat `annual_amount` (no fraction)
        # does NOT need value_share and stays out of this block, byte-identical
        # to a household with no appreciation/sale/financing (DP#32).
        #
        # Issue #1011: a property whose purchase declares `funding_options`
        # ALSO needs these shares carried, because the funding sweep rebuilds
        # `net_equity`/`secured_share` PER candidate funding method (an
        # all-cash option draws the full value; a mortgage option draws only
        # the down payment). Without `value_share`/`secured_share` the overlay
        # could not recompute the down payment for a candidate when the
        # property has no `financing`/`appreciation_rate`/`sale` of its own --
        # so the carry condition is extended to include `has_funding_options`.
        has_financing = financing is not None
        carrying_costs = prop.get("carrying_costs")
        cc_fraction = (carrying_costs.get("fraction_of_value")
                       if carrying_costs is not None else None)
        has_funding_options = funding_options is not None
        if (appreciation_rate is not None or sale is not None or has_financing
                or cc_fraction is not None or has_funding_options):
            value_share = prop["value"]["amount"] * couple_share
            entry["value_share"] = value_share
            entry["secured_share"] = secured * couple_share
            acb = prop.get("acb")
            if acb is not None:
                entry["acb_share"] = acb * couple_share
            else:
                # null ACB = no accrued gain yet (bought at value): the gain
                # inputs collapse to value_share - value_share == 0, the correct
                # disposition gain for a just-acquired property (DP#32).
                entry["acb_share"] = value_share
            if appreciation_rate is not None:
                entry["appreciation_rate"] = appreciation_rate
        # Issue #1010 (epic #956): a property's RECURRING carrying costs
        # (property tax, maintenance, insurance) charged each year over the
        # ownership window through the solvency waterfall. The flat
        # `annual_amount` is taken at the couple's ownership share (mirroring
        # `purchase.closing_costs` and `sale.selling_costs`); `fraction_of_value`
        # is carried RAW and applied to the couple-share gross value in the fold
        # (value_share already embeds the couple share, so the fraction needs no
        # extra scaling). Both components are read with explicit `is not None`
        # tests -- never `cc.get(k) or 0` (DP#32: a real $0 / 0.0 is a legitimate
        # zero, not an unknown). Emitted ONLY when `carrying_costs` is declared
        # and at least one component is non-null, so an absent block (and a
        # declared `{}`) round-trips byte-identical to today -- the property is
        # free to carry, exactly as before (DP#32). Forbidden on the principal
        # residence by the schema (its costs live in annual_living_costs), so a
        # non-null block never reaches here for kind=principal.
        if carrying_costs is not None:
            cc_amount = carrying_costs.get("annual_amount")
            cc_entry: Dict[str, Any] = {}
            if cc_amount is not None:
                cc_entry["annual_amount"] = cc_amount * couple_share
            if cc_fraction is not None:
                cc_entry["fraction_of_value"] = cc_fraction
            if cc_entry:
                entry["carrying_costs"] = cc_entry
        # Issue #696 (epic #690 bite 5): a dated MID-HORIZON PURCHASE. When
        # declared, the property is BOUGHT on `purchase.date` rather than held
        # from year 0: it contributes no equity/rent/CCA before that calendar
        # year, and in the purchase year the household funds the down payment
        # (`net_equity` -- value less the mortgage originated against it, at the
        # couple's share) plus the couple's share of `closing_costs` out of
        # cash (simulation._prologue / _property_equity_for_year). The calendar
        # year is derived from the date exactly as a cash_flow's is
        # (int(date[:4]), see the cash_flows map). Emitted only when `purchase`
        # is present, so every property held from year 0 round-trips
        # byte-identically to #692 (DP#32).
        purchase = prop.get("purchase")
        if purchase is not None:
            # Issue #956 bite C: the calendar year is read from a declared
            # numeric `year` leaf when present (a sweepable alternative to
            # `date`, ranged via sensitivity.sweeps), and derived from `date`
            # otherwise (int(date[:4]), the same spelling a cash_flow uses). An
            # explicit `is not None` test -- never `purchase.get("year") or
            # int(...)` (DP#32: `0` would be unrepresentable, and a year of 0
            # is a real value the schema permits). Exactly one of `year`/`date`
            # is guaranteed present by the schema's oneOf.
            pyear = (purchase["year"] if purchase.get("year") is not None
                     else int(purchase["date"][:4]))
            entry["purchase"] = {
                "year": pyear,
                "closing_costs": purchase["closing_costs"] * couple_share,
            }
            # Issue #967: a MORTGAGE originated against this property at the
            # purchase year. When `purchase.financing` is declared, the
            # property is equity-financed only for the DOWN PAYMENT (value -
            # mortgage_amount, couple share -- already `net_equity` above);
            # the mortgage funds the rest, originates as a serviced secured
            # liability in `pyear`, and its interest is deductible when the
            # property is a rental (ITA s.20(1)(c)) and NON-deductible for a
            # recreational/personal property. The deductibility follows the
            # property's `kind` -- a cottage (kind=recreational) has no
            # `rental` block, so its mortgage interest never reaches the
            # rental deduction fold, by construction. Carried on the entry's
            # `purchase` block (mirroring how the year/closing_costs ride
            # there) so the fold's servicing rule can find it. The principal
            # is taken at the couple's share, mirroring liability.balance and
            # the `secured` netting above. Emitted only when `financing` is
            # present, so an equity-financed purchase round-trips byte-
            # identical to #696 (DP#32). `owner_roles` is the couple's
            # owner->role split (mirroring the rental/sale mapping) so the
            # servicing + deduction can apportion to each taxed member.
            if financing is not None:
                owner_roles = {
                    role: shares.get(pid, 0.0)
                    for role, pid in (("primary", primary_id),
                                      ("spouse", spouse_id))
                    if pid is not None and shares.get(pid, 0.0) > 0
                }
                financed_principal_share = financing["mortgage_amount"] * couple_share
                # Issue #967: precompute the mortgage's ANNUAL amortization
                # schedule from origination to payoff (or the horizon), so the
                # servicing rule, the rental interest deduction, and the
                # balance-sheet total_debt all read ONE schedule (DP#9 -- one
                # spelling, never three computations that could drift). The
                # schedule covers `projection_years` from `pyear`; a mortgage
                # whose term outlives the horizon is capped at the horizon (the
                # fold never reaches past it, so an unserviced trailing slice
                # would be dead data). Built by the pure `_annual_amortization_
                # schedule` helper, which reuses the standard annuity formula
                # (monthly_payment) -- NOT a new amortization engine.
                schedule = _annual_amortization_schedule(
                    financed_principal_share, financing["rate"],
                    financing["amortization_years"], pyear,
                    projection_years)
                entry["purchase"]["financing"] = {
                    "mortgage_amount": financed_principal_share,
                    "rate": financing["rate"],
                    "rate_type": financing["rate_type"],
                    "amortization_years": financing["amortization_years"],
                    "origination_year": pyear,
                    "deductible": prop["kind"] == "rental",
                    "owner_roles": owner_roles,
                    "schedule": schedule,
                }
            # Issue #1011: carry the declared `funding_options` (the SWEEP form
            # of `financing`) onto the annual-side purchase block, plus the
            # `funding_recompute` inputs the per-option overlay needs to
            # materialize a `financing` block for any chosen candidate WITHOUT
            # re-deriving the owner structure / kind / horizon here a second
            # time. `value_share` (carried above because has_funding_options
            # extends the carry condition) plus `secured_base` let the overlay
            # recompute net_equity/secured_share per option; `owner_roles`/
            # `deductible`/`projection_years` let it rebuild the financing
            # block identically to how the fixed-`financing` branch above
            # builds it (DP#9: the schedule itself comes from the SAME pure
            # `_annual_amortization_schedule` helper, never a second spelling).
            # `secured_base` is the couple-share of the NON-financing secured
            # debt (year-0 mortgages/HELOCs collateralized on this property),
            # invariant across funding candidates. Emitted only when
            # `funding_options` is declared, so a property with neither
            # `financing` nor `funding_options` round-trips byte-identical to
            # #967/#696 (DP#32). The engine never reads `funding_options` or
            # `funding_recompute` itself -- they are sweep metadata consumed
            # only by scenario_discovery / optimize.run_property_funding_
            # exploration (DP#18: a write that reaches a real reader).
            if funding_options is not None:
                owner_roles = {
                    role: shares.get(pid, 0.0)
                    for role, pid in (("primary", primary_id),
                                      ("spouse", spouse_id))
                    if pid is not None and shares.get(pid, 0.0) > 0
                }
                entry["purchase"]["funding_options"] = [
                    dict(opt) for opt in funding_options]
                entry["purchase"]["funding_recompute"] = {
                    "secured_base": (secured - financed_principal) * couple_share,
                    "owner_roles": owner_roles,
                    "deductible": prop["kind"] == "rental",
                    "projection_years": projection_years,
                }
        # Issue #956 bite B: a dated mid-horizon voluntary SALE -- the
        # symmetric inverse of the #696 purchase above. When declared, the
        # property LEAVES the balance sheet in the calendar year of `sale.date`
        # (its equity gates to zero from that year on, see
        # `_property_equity_for_year`), and the tax + proceeds layer built on
        # top of this bite invests the net proceeds (value_share less
        # secured_share, selling_costs, and disposition tax) into the
        # portfolio. The calendar year is derived from the date exactly as a
        # cash_flow's is (int(date[:4]), mirroring the purchase mapping).
        # `selling_costs` is read explicitly -- never `sc or 0.0` (DP#32 forbids
        # `x or DEFAULT`): a null `selling_costs` is a real $0 in disposition
        # costs, distinct from a sale that has not been priced. The schema
        # declares it `money | null` (a bare number, the same shape as
        # `closing_costs` above), so it is read directly, not as an {amount}
        # object. Emitted only when `sale` is present, so a property held to the
        # horizon round-trips byte-identical to #692/#696 (DP#32).
        #
        # The tax + proceeds layer (this bite's sale-core) prices the
        # disposition gain per OWNER: Canada has no joint filing, so each
        # spouse reports their share of the gain on their OWN return and it
        # bands against THAT spouse's taxable income (the same per-owner split
        # the rental `owner_roles` field below drives for net rental income).
        # The gain inputs above (value_share / secured_share / acb_share) are
        # the COUPLE'S aggregate share; the per-owner split is carried here as
        # `owner_roles` (mirroring the rental mapping's spelling exactly) so
        # the disposition rule can apportion the gain to each owner without
        # re-deriving the owner structure from the contract (the mapped entry
        # carries no `owner` field). Each role's fraction is its declared %
        # of the property (a couple owning a cottage 50/50 -> {'primary': 0.5,
        # 'spouse': 0.5}); the fractions sum to couple_share, so weighting the
        # couple-share gain by role_frac/couple_share recovers each owner's
        # share of the gain. Emitted only when `sale` is present, so a property
        # held to the horizon round-trips byte-identically to #692/#696 (DP#32).
        if sale is not None:
            sc = sale.get("selling_costs")
            selling_costs = (sc if sc is not None else 0.0) * couple_share
            # Issue #956 bite C: the sale calendar year is read from a declared
            # numeric `year` leaf when present (a sweepable alternative to
            # `date`), and derived from `date` otherwise (int(date[:4]),
            # mirroring the purchase mapping above). An explicit `is not None`
            # test -- never `sale.get("year") or int(...)` (DP#32). Exactly one
            # of `year`/`date` is guaranteed present by the schema's oneOf.
            syear = (sale["year"] if sale.get("year") is not None
                     else int(sale["date"][:4]))
            owner_roles = {
                role: shares.get(pid, 0.0)
                for role, pid in (("primary", primary_id), ("spouse", spouse_id))
                if pid is not None and shares.get(pid, 0.0) > 0
            }
            # Issue #956 bite B (sale-core): carry the property's PRE designation
            # periods so the disposition rule can apportion the gain (ITA
            # s.40(2)(b)) -- a property designated as a principal residence for
            # some years shelters the gain apportioned to those years. The
            # periods are carried RAW (the same shape `designated_years` reads);
            # the rule resolves them to the designated-year SET capped at the
            # sale year (a property sold mid-horizon cannot designate years
            # after its sale). A property with no designation carries [] -- the
            # rule's `taxable_gain_fraction(0, ...) == 1.0` (fully taxable).
            entry["sale"] = {
                "year": syear,
                "selling_costs": selling_costs,
                "owner_roles": owner_roles,
                "designated_principal_residence_years":
                    prop.get("designated_principal_residence_years", []),
            }
            # Issue #969: carry the family-level PRE window onto the sale so
            # the disposition rule prices the gain against the FAMILY
            # denominator (ITA s.40(2)(b)), not this property's own span in
            # isolation (the single-property approximation that over-shelters
            # a second property's gain). Carried ONLY when a genuine family
            # contest exists (>=2 properties with designations); absent is the
            # byte-identical legacy sentinel -- a single-property household
            # falls back to its own window, unchanged (DP#32).
            if family_pre_window is not None:
                entry["sale"]["family_pre_window"] = family_pre_window
        # Issue #693 (epic #690 bite 2): a rental property produces NET RENTAL
        # INCOME (gross rent - operating expenses, ITA/CRA T776) taxable at the
        # owner's marginal rate, and the mortgage interest on it is DEDUCTIBLE
        # (ITA s.20(1)(c)) because the property earns income. Carry the declared
        # T776 facts, the annual interest on the debt secured against THIS
        # property (balance x rate -- the same facilities netted from equity
        # above), and the couple's OWNER->ROLE split so the fold can attribute
        # the income/deduction to each taxed member. `rental` is emitted only
        # for a kind=rental property that actually declares `rental` facts;
        # absent otherwise, so a cottage (kind=recreational) carries equity only
        # and every non-rental household round-trips byte-identically (DP#32).
        rental = prop.get("rental")
        if prop["kind"] == "rental" and rental is not None:
            mortgage_interest = sum(
                liab["balance"]["amount"] * liab["rate"] for liab in secured_liabs
            )
            owner_roles = {
                role: shares.get(pid, 0.0)
                for role, pid in (("primary", primary_id), ("spouse", spouse_id))
                if pid is not None and shares.get(pid, 0.0) > 0
            }
            entry["rental"] = {
                "gross_rent_annual": rental["gross_rent_annual"],
                "expenses_annual": rental["expenses_annual"],
                "mortgage_interest_annual": mortgage_interest,
                "owner_roles": owner_roles,
            }
            # Issue #967: a financed rental's mortgage interest is DEDUCTIBLE
            # under s.20(1)(c), but the mortgage ORIGINATES at the purchase
            # year -- so the static `mortgage_interest_annual` above (built
            # from year-0 secured_liabs) is $0 for a financed rental (the
            # mortgage did not exist at year 0). The per-year interest lives
            # on the financing block's precomputed schedule; carry a REFERENCE
            # to it on the rental block so `simulation._rental_income_for` can
            # add the dynamic interest for `sim_year` to the deduction. A
            # rental with no financing (held from year 0, or equity-financed
            # at purchase) has no `financing` here -> the rental fold reads
            # the static `mortgage_interest_annual` only, byte-identical to
            # #693 (DP#32). A cottage (kind=recreational) has no `rental`
            # block at all, so its financed interest never reaches the rental
            # deduction -- non-deductible by construction, as required.
            fin_ref = (entry.get("purchase", {}).get("financing")
                       if purchase is not None else None)
            if fin_ref is not None:
                entry["rental"]["financing_schedule"] = fin_ref["schedule"]
            # Issue #694 (epic #690 bite 3): an optional Capital Cost Allowance
            # election. When declared, the fold depreciates the building against
            # rental income each year (lowering taxable income, tracking the
            # UCC) and RECAPTURES the previously-claimed CCA as ordinary income
            # at the estate's deemed disposition (ITA s.13(1)). Carried through
            # as declared -- a rental the couple owns whole, so the capital
            # cost / UCC are the couple's depreciable figures; `fmv_at_disposition`
            # is the couple's disposition proceeds (value x couple_share, the SAME
            # figure `_map_estate` taxes the capital gain on), so the recapture
            # ceiling and the estate gain share one property valuation. Emitted
            # only when `cca` is present, so a rental without a CCA election
            # round-trips byte-identically to #693 (DP#32).
            cca = rental.get("cca")
            if cca is not None:
                entry["rental"]["cca"] = {
                    "rate": cca["rate"],
                    "capital_cost": cca["capital_cost"],
                    "opening_ucc": cca["opening_ucc"],
                    "fmv_at_disposition": prop["value"]["amount"] * couple_share,
                }
            # Issue #697 (epic #690 bite 6): a SHORT-TERM rental (Airbnb-style)
            # is a legally and fiscally different animal. When `short_term` is
            # declared, the Canada jurisdiction module rules on its LEGALITY
            # (countries/canada/short_term_rental) and REFUSES to map any income
            # for an STR in a banned borough or without a CITQ registration --
            # a loud refusal at load, never a silent "assume legal" (DP#25: the
            # rule lives in the jurisdiction module, this adapter only invokes
            # it; DP#32: absence of confirmed legality fails, it is not guessed).
            # A PERMITTED STR carries its business-income classification (ITA s.9,
            # not passive T776 property income) and the GST/HST small-supplier
            # flag ($30k, ETA s.148) so the fold can surface them; its net income
            # still rides the SAME s.20(1)(c)/T776 net-income fold as a long-term
            # rental (business income is ordinary income too, DP#9). Emitted only
            # when `short_term` is present, so a plain rental round-trips
            # byte-identically to #693 (DP#32).
            short_term = rental.get("short_term")
            if short_term is not None:
                from countries.canada.short_term_rental import (
                    classify_str_income, require_str_permitted)
                require_str_permitted(
                    short_term["jurisdiction"], short_term["citq_registered"])
                effect = classify_str_income(
                    rental["gross_rent_annual"], rental["expenses_annual"],
                    mortgage_interest)
                entry["rental"]["short_term"] = {
                    "jurisdiction": short_term["jurisdiction"],
                    "citq_registered": short_term["citq_registered"],
                    "income_type": effect.income_type,
                    "gst_hst_registration_required":
                        effect.gst_hst_registration_required,
                }
        owned.append(entry)
    return owned


def _ownership_share(doc: Dict, kind: str, person_id: str,
                     couple: List[str], default_share: float) -> float:
    """The fraction of the couple's total ``kind`` balance owned by
    ``person_id``. ``default_share`` applies only when the couple holds NOTHING
    of this kind -- there is genuinely no split to compute, not a value being
    coerced (DP#13/DP#32)."""
    total = 0.0
    mine = 0.0
    for acc in doc.get("accounts", []):
        if acc["kind"] != kind:
            continue
        shares = _owner_shares(acc["owner"])
        amount = acc["balance"]["amount"]
        for pid, frac in shares.items():
            if pid not in couple:
                continue
            total += amount * frac
            if pid == person_id:
                mine += amount * frac
    if total <= 0:
        return default_share
    return mine / total


def _primary_dies_first(doc: Dict, mortality: Dict[str, Dict],
                        primary_id: str, spouse_id: Optional[str]) -> bool:
    """Whose terminal return do the rolled assets land on? Decided by the
    DECLARED mortality beliefs (assumptions.mortality), not assumed.

    Compares the two spouses' assumed death DATES (derived from
    assumed_death_age + birth_date when a date isn't given directly). With no
    spouse there is only one death, so the primary is trivially "first"; the
    rollover fraction will be 0 anyway (nobody to roll to)."""
    if spouse_id is None:
        return True
    p_date = _assumed_death_date(doc, mortality, primary_id)
    s_date = _assumed_death_date(doc, mortality, spouse_id)
    if p_date is None or s_date is None:
        # Both must be stated for the comparison to mean anything. The schema
        # makes assumptions.mortality a list, not a required entry per person,
        # so this is genuinely reachable -- and a coin-flip guess about WHO DIES
        # FIRST silently relocates a seven-figure tax base between two returns.
        raise ContractAdaptationError(
            f"assumptions.mortality must state a death age/date for BOTH "
            f"spouses ({primary_id!r} and {spouse_id!r}) -- the estate model "
            f"needs to know who dies first to know whose terminal return the "
            f"rolled-over assets land on (ITA s.70(6)/s.146(8.1)). Guessing "
            f"would silently relocate the household's largest tax base."
        )
    return p_date <= s_date


def _assumed_death_date(doc: Dict, mortality: Dict[str, Dict],
                        person_id: str) -> Optional[str]:
    """A person's assumed death date (ISO), from an explicit
    ``assumed_death_date``, or derived from ``assumed_death_age`` + birth_date
    (DP#1: dates, not ages, drive the comparison)."""
    m = mortality.get(person_id)
    if m is None:
        return None
    if m.get("assumed_death_date") is not None:
        return m["assumed_death_date"]
    age = m.get("assumed_death_age")
    if age is None:
        return None
    birth = _people_by_id(doc)[person_id].get("birth_date")
    if birth is None:
        return None
    b = _date.fromisoformat(birth)
    return b.replace(year=b.year + age).isoformat()


def _horizon_date(doc: Dict, primary_id: str) -> Optional[str]:
    """The projection's terminal date -- when the horizon person reaches
    ``decisions.horizon.until_age``. Used to decide whether a TERM life policy
    is still in force at the estate valuation."""
    horizon = doc["decisions"]["horizon"]
    person = _people_by_id(doc).get(horizon["person"])
    if person is None or not person.get("birth_date"):
        return None
    b = _date.fromisoformat(person["birth_date"])
    return b.replace(year=b.year + horizon["until_age"]).isoformat()


# Issue #823: account kinds whose balances the engine grows as one aggregate
# pot in `apply_registered_growth` / `apply_non_reg_growth`. A per-account
# `expected_return` override is blended (balance-weighted) into its pot so a
# flagged account grows at its own rate while the rest of the pot uses the
# global return_model rate. Kinds not in this set (resp/dcpp/dbpp/rrif) are
# either not grown at the equity rate or are out of scope for #823.
_GROWTH_POT_KINDS = frozenset(
    {"rrsp", "spousal_rrsp", "tfsa", "fhsa", "non_reg", "lira", "lif"})


def _resolve_product_rules(product):
    """Issue #826 (DP#7/#10/#12/#16): resolve an account's ``product`` flag to
    the product module's rules (expected_return / locked_until defaults).

    Returns ``None`` when ``product`` is None/empty (a generic account with no
    product-module rules -- today's behaviour). The resolution goes through
    ``countries.canada.fonds_ftq.resolve_product`` (imported lazily so this
    jurisdiction-agnostic mapping module does not import the Canada package at
    module load -- DP#25). An unknown product id raises ``ValueError`` from
    the product module (a typo / not-yet-implemented product must not silently
    fall back to generic -- DP#32).
    """
    if not product:
        return None
    from countries.canada.fonds_ftq import resolve_product as _resolve
    return _resolve(product)


def _map_account_overrides(doc: Dict) -> Dict[str, Any]:
    """Issue #823/#691: collect per-account `expected_return`, `locked_until`
    and `mer` overrides into pot-keyed structures the growth and solvency rules
    read.

    Returns ``{'return_overrides': {kind: {override_balance, weighted_rate_sum}},
    'locked': {kind: [{balance, unlock_age, owner_birth_year}]},
    'mer_drag': {kind: {mer_balance, weighted_mer_sum}}}``.

    - ``mer_drag[kind]`` (issue #691): the summed balance of accounts of ``kind``
      that declared a `mer` fee, plus the balance-weighted fee sum
      (`sum(balance * mer)`). The growth rule subtracts this from the pot's
      gross rate: ``net = gross - weighted_mer_sum / pot_total`` -- so a declared
      fee reduces the compounded balance (composing on top of the expected_return
      blend). A null/absent `mer` records nothing (fee-free global rate, golden);
      an explicit 0.0 is recorded (a declared fact, DP#32) but moves no rate.

    - ``return_overrides[kind]``: the summed balance of accounts of ``kind``
      that declared an `expected_return`, plus the balance-weighted rate sum
      (`sum(balance * rate)`). The growth rule blends this into the pot at
      runtime: ``pot_rate = (weighted_rate_sum + (pot_total - override_balance)
      * global) / pot_total``. Accounts without an override contribute at the
      global rate (the default; preserves today's behaviour when no account
      declares one -- golden).
    - ``locked[kind]``: one entry per locked account of ``kind``, carrying its
      balance, its unlock AGE (DP#1: age is date-computed from the owner's
      birth_year, never a hardcoded constant), and the owner's birth_year. The
      solvency rule excludes a locked balance from the liquidation waterfall
      in any year the owner has not yet reached `unlock_age`; after that age
      it is liquid. A `locked_until.date` is converted to the owner's age at
      that date (a date condition and an age condition are the same fact once
      the owner's birth_year is known -- DP#1).

    DP#32: an absent `expected_return` / `locked_until` is null (not zero, not
    a fallback) -- the caller that declares none gets today's global-rate,
    fully-liquid behaviour, which is what keeps the golden invariant unchanged.

    Issue #826 (DP#7/#10/#12/#13): an account carrying a `product` flag (e.g.
    'fonds_ftq') resolves the product module's well-known rules as DEFAULTS
    for expected_return / locked_until -- so a household flags the product
    and does NOT restate its rules. An EXPLICIT account.expected_return /
    account.locked_until on the same account OVERRIDES the product default
    (DP#13: a declared value wins over a fallback). The product defaults feed
    the SAME #823 downstream machinery; this function does not duplicate it.
    """
    people = _people_by_id(doc)
    return_overrides: Dict[str, Dict[str, float]] = {}
    locked: Dict[str, List[Dict[str, Any]]] = {}
    mer_drag: Dict[str, Dict[str, float]] = {}
    for acc in doc.get("accounts", []):
        kind = acc["kind"]
        if kind not in _GROWTH_POT_KINDS:
            continue
        amount = acc["balance"]["amount"]
        # Issue #826 (DP#7/#10/#12/#13): a product flag resolves the
        # product module's well-known rules as DEFAULTS for expected_return /
        # locked_until. An EXPLICIT account.expected_return / account.locked_until
        # wins over the product default (DP#13: a declared value beats a
        # fallback). So the effective values are: explicit if declared, else
        # the product default, else None (today's global-rate / fully-liquid
        # behaviour -- golden when no product is declared either).
        product_rules = _resolve_product_rules(acc.get("product"))
        er = acc.get("expected_return")
        if er is None and product_rules is not None:
            er = product_rules.expected_return
        if er is not None:
            entry = return_overrides.setdefault(kind, {"override_balance": 0.0,
                                                        "weighted_rate_sum": 0.0})
            entry["override_balance"] += amount
            entry["weighted_rate_sum"] += amount * er
        # Issue #691: a per-account MER (management-expense-ratio) fee, summed
        # balance-weighted into its pot so the growth rule subtracts it from the
        # gross rate (net = gross - sum(balance*mer)/pot_total). DP#32: an
        # explicit 0.0 IS a declared fact (fee-free), recorded here (mer_balance
        # counted, weighted_mer_sum contributes 0) and distinct from a null/
        # absent MER, which records nothing and leaves today's global-rate
        # behaviour untouched (golden). This is the ONE engine-read fee spelling
        # (DP#8); it composes on top of the #823 expected_return blend above.
        mer = acc.get("mer")
        if mer is not None:
            m = mer_drag.setdefault(kind, {"mer_balance": 0.0,
                                           "weighted_mer_sum": 0.0})
            m["mer_balance"] += amount
            m["weighted_mer_sum"] += amount * mer
        lu = acc.get("locked_until")
        if lu is None and product_rules is not None:
            lu = product_rules.locked_until
        if lu is not None:
            # The owner whose age gates the unlock. FTQ shares are registered
            # (single owner); for a joint owner_ref the first joint holder is
            # the owner of record (#601's owner shape).
            shares = _owner_shares(acc.get("owner"))
            owner_id = next(iter(shares), None)
            owner = people.get(owner_id) if owner_id else None
            if owner is None:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={kind}) declares "
                    f"locked_until but its owner {owner_id!r} is not in the "
                    f"document's people -- the unlock AGE cannot be computed "
                    f"without a birth date (DP#1/DP#32, issue #823)."
                )
            birth_date = owner.get("birth_date")
            if not birth_date:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={kind}) declares "
                    f"locked_until but its owner {owner_id!r} has no "
                    f"birth_date -- the unlock AGE cannot be computed from "
                    f"a missing birth date (DP#1/DP#32, issue #823)."
                )
            owner_birth_year = int(birth_date[:4])
            if "age" in lu and lu["age"] is not None:
                unlock_age = int(lu["age"])
            elif "date" in lu and lu["date"] is not None:
                unlock_age = int(lu["date"][:4]) - owner_birth_year
            else:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} declares locked_until with neither "
                    f"`age` nor `date` (issue #823) -- one is required."
                )
            locked.setdefault(kind, []).append(
                {"balance": amount, "unlock_age": unlock_age,
                 "owner_birth_year": owner_birth_year})
    return {"return_overrides": return_overrides, "locked": locked,
            "mer_drag": mer_drag}


#: Issue #917: the registered kinds whose composition the engine reads back --
#: exactly the pots PortfolioConfig.registered_wht_drag (#641/#912) and the
#: asset-location optimizer (#473) act on. non_reg is threaded by its own block
#: (its composition reaches the engine through the non-reg after-tax path).
#: Issue #912 added fhsa/lira/lif: registered_wht_drag now reads their holdings
#: and the fhsa/lira_lif growth rules subtract the resulting WHT drag, so
#: threading their composition here lands on a read key (no longer a dead write,
#: DP#18).
_REGISTERED_COMPOSITION_KINDS = ("rrsp", "tfsa", "fhsa", "lira", "lif")


def _blend_registered_holdings(accs: List[Dict]) -> List[Dict]:
    """Combine several same-kind registered accounts' holdings into ONE
    balance-weighted holdings list (issue #917).

    The engine holds a single rrsp/tfsa pot (a couple's two rrsp accounts are
    summed into one balance), so their two composition declarations must blend
    into one. Each account contributes its holdings scaled by its share of the
    pot's total balance, so the blended foreign intensity is the true
    dollar-weighted average -- not the concatenated SUM, which would double a
    per-unit rate when both accounts hold the same product. When every balance
    is 0 the pot is empty and the rate is inert either way; the accounts are
    then weighted equally so the composition stays well-defined (no div-by-0).
    """
    total = sum(a["balance"]["amount"] for a in accs)
    n = len(accs)
    combined: List[Dict] = []
    for acc in accs:
        bal = acc["balance"]["amount"]
        share = (bal / total) if total > 0 else (1.0 / n)
        for h in acc.get("holdings", []):
            combined.append({"product": h["product"], "weight": h["weight"] * share})
    return combined


def _registered_composition_accounts(doc: Dict, products: Dict) -> Dict[str, Dict]:
    """Issue #917: ``{kind: {composition, yield}}`` for every registered pot in
    ``doc`` that declares product holdings, derived from those holdings.

    Registered pots that declare no holdings contribute no entry, so an absent
    or holdings-free contract is a strict no-op (DP#32): the pots keep the flat
    gross rate and no placement decision exists. The product->composition
    derivation reuses ``PortfolioConfig.from_dict``/``to_dict`` (one derivation
    model, DP#9/DP#24) so the composition the WHT drag and the optimizer read is
    the exact one the engine would derive from the same holdings.
    """
    accounts_in: Dict[str, Dict] = {}
    for kind in _REGISTERED_COMPOSITION_KINDS:
        accs = [a for a in doc.get("accounts", [])
                if a["kind"] == kind and a.get("holdings")]
        if not accs:
            continue
        accounts_in[kind] = {"holdings": _blend_registered_holdings(accs)}
    if not accounts_in:
        return {}
    # Lazy countries.canada import (mirrors this adapter's fonds_ftq import):
    # resolving a declared product to its composition IS the jurisdiction's
    # product model, and reusing the engine's own derivation is DP#9.
    from countries.canada.portfolio import PortfolioConfig
    derived = PortfolioConfig.from_dict(
        {"products": products, "accounts": accounts_in}).to_dict()["accounts"]
    return {kind: {"composition": acct["composition"], "yield": acct["yield"]}
            for kind, acct in derived.items()}


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
    horizon_end = _horizon_end_year(doc, primary_id)
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

    # #647: every rrsp/tfsa/spousal_rrsp/fhsa account's opening balance,
    # attributed to its owner or refused loudly -- computed once, up front,
    # so the refusal happens before any partial mapping is built.
    registered_balances = _map_registered_balances(
        doc, primary_id, spouse_id, child_ids, extra_adult_ids)

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

    prop_cfg: Dict[str, Any] = {
        "house_value": principal["value"]["amount"] if principal else 0,
    }
    # Issue #963 (epic #956 bite F): the principal residence's REAL annual
    # appreciation rate, mirroring Bite A's `appreciation_rate` on a
    # non-principal property. The schema declares this leaf on EVERY property
    # (incl. kind=principal), so it is already permitted on the principal --
    # only the MAPPER + the consumers needed to honor it. Carried only when
    # declared so a household with no rate (incl. the golden fixture, whose
    # legacy `property` dict never carries this key) round-trips byte-identical
    # to today (DP#32): the consumers' absence-test returns the static
    # `house_value` and never read `appreciation_rate`. The base value is
    # `house_value` itself (the principal is held from the projection start,
    # never a dated mid-horizon purchase); the consumers compound
    # `house_value * (1 + rate) ** (cal_year - start_year)`. A sweepable
    # numeric leaf: range it via sensitivity.sweeps (e.g.
    # properties.N.appreciation_rate: [0, 0.03, 0.05]) so a sell/keep
    # conclusion is robust to the home-appreciation assumption rather than
    # hostage to a static-value guess that systematically favours selling.
    if principal is not None:
        appreciation_rate = principal.get("appreciation_rate")
        if appreciation_rate is not None:
            prop_cfg["appreciation_rate"] = appreciation_rate
    if mortgage:
        prop_cfg["mortgage_balance"] = mortgage["balance"]["amount"]
        prop_cfg["mortgage_rate"] = mortgage["rate"]
        if "amortization" in mortgage:
            prop_cfg["amortization_years"] = mortgage["amortization"]["years"]
            # mortgage["amortization"]["payment_monthly"] / .renewal_date /
            # .term_start_date are NOT mapped here (epic #603 Track C Phase
            # 2b finding): the legacy keys they used to target
            # (property.current_payment_monthly/.renewal_date/
            # .contract_start_date) were confirmed dead and deleted from the
            # legacy schema in Phase 2a (test_schema_coverage.py's history);
            # SimulationConfig.from_dict never read them either. Mapping a
            # real contract fact onto a key nothing reads is the DP#18/DP#32
            # "written, not applied" failure this epic exists to end -- so
            # this mapper does not populate them (measured, not asserted: see
            # tests/architecture/test_contract_reachability.py).
        # Issue #1075 (data-model half): the sum of the balances of every
        # kind=mortgage tranche whose new `deductible` flag is set (their
        # interest is deductible under ITA s.20(1)(c) -- borrowed for an
        # income-producing non-registered investment), and -- alongside it --
        # those tranches' EXACT annual interest (each balance * its OWN
        # rate). Surfaced on the config (SimulationConfig
        # .deductible_mortgage_balance / .deductible_mortgage_interest) so
        # the s.20(1)(c) interest pricing (issue #850) can price the
        # declared tranches' TRUE interest instead of tracing it. The
        # interest is NOT deductible_mortgage_balance * mortgage_rate: the
        # rate here is the balance-weighted average of ALL tranches, which
        # coincides with the deductible tranches' own-rate sum only when
        # every tranche shares one rate (DP#32: never price deductible
        # interest off a blended rate). Both emitted only when > 0: a
        # household with no deductible tranche (the golden fixture and every
        # pre-#1075 contract) round-trips byte-identical (DP#32 -- absence
        # of the flag is "not deductible", never a fabricated zero key).
        deductible_mortgage_balance = mortgage.get("deductible_balance", 0.0)
        if deductible_mortgage_balance > 0:
            prop_cfg["deductible_mortgage_balance"] = deductible_mortgage_balance
        deductible_mortgage_interest = mortgage.get("deductible_interest", 0.0)
        if deductible_mortgage_interest > 0:
            prop_cfg["deductible_mortgage_interest"] = deductible_mortgage_interest

    # Issue #956 bite E (principal-residence disposition): a declared
    # mid-horizon SALE of the PRINCIPAL residence. The principal is
    # deliberately excluded from `_map_owned_properties` (its value reaches
    # the annual side via `house_value`/LTV/charge math, not via
    # `property_equities` in total_assets), so Bite B's `property_disposition`
    # rule cannot sell it -- the principal's sale is a separate disposition
    # with its own rule. The contract surface reuses the SAME `property_sale`
    # schema block Bite B defines (the principal is a `kind="principal"`
    # property in `doc["properties"]`, and the schema permits `sale` on any
    # property); this mapper carries it onto `prop_cfg["principal_sale"]`
    # (the principal's own config seam, distinct from the non-principal
    # `properties[]` path). The sale year is sweepable via the same numeric
    # `year` leaf Bite C added (sensitivity.sweeps.properties.N.sale.year),
    # so the optimizer can rank sell-TIMING. Exactly the same shape Bite B
    # carries on a non-principal `sale`: {year, selling_costs, owner_roles,
    # designated_principal_residence_years, value_share, acb_share} -- the
    # rule reads these off the config. `secured_share` is NOT carried here:
    # the principal's mortgage AMORTIZES (the non-principal path's static
    # `secured_share` is a snapshot of a mortgage the engine does not
    # amortize), so the discharged debt is the LIVE year-N balance the rule
    # reads off `ws` at the sale year, not a config-time snapshot.
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

    principal_sale = _map_principal_sale(
        principal, mortgage, heloc, primary_id, spouse_id,
        family_pre_window=family_pre_window)
    if principal_sale is not None:
        prop_cfg["principal_sale"] = principal_sale

    # issue #689: does the credit facility secure against THIS property's
    # charge at all? null collateral (unsecured) or collateral pointing at a
    # different property both mean "no" -- an unsecured line is genuinely
    # ADDITIONAL borrowing capacity, outside the registered charge, and it
    # must not be folded into a check it was never part of (that would
    # understate the household's real, unsecured capacity in exactly the
    # direction #689 exists to fix).
    credit_facility_secured_here = bool(
        credit_facility and principal and credit_facility.get("collateral") == principal["id"]
    )

    if heloc or credit_facility_secured_here:
        # issue #664/#689: a readvanceable mortgage, its HELOC, and any
        # SECURED line of credit against the same property are carved out
        # of ONE registered charge with ONE combined limit -- refuse loudly
        # (DP#32) if the document itself declares secured debt that already
        # exceeds it, rather than silently modeling a >80% LTV facility.
        # Checked here, at the ONE contract-loading boundary, so every
        # caller of to_internal_config gets the refusal regardless of
        # whether an LTV overlay is ever applied downstream
        # (apply_ltv_overlay/apply_overlay re-check after any overlay that
        # grows the mortgage further, per their own docstrings).
        from simulation_config import (
            charge_limit as _charge_limit, heloc_revolving_limit as _heloc_revolving_limit,
            OSFI_B20_CHARGE_LTV_MAX, OSFI_B20_REVOLVING_LTV_MAX, _CHARGE_TOLERANCE,
        )
        house_value = principal["value"]["amount"]
        declared_mortgage = mortgage["balance"]["amount"] if mortgage else 0
        declared_heloc_limit = heloc["limit"] if heloc else 0
        declared_cf_limit = credit_facility["limit"] if credit_facility_secured_here else 0
        combined_limit = _charge_limit(house_value, OSFI_B20_CHARGE_LTV_MAX)
        combined_secured = declared_mortgage + declared_heloc_limit + declared_cf_limit
        if combined_secured > combined_limit + _CHARGE_TOLERANCE:
            raise ContractAdaptationError(
                f"Declared mortgage (${declared_mortgage:,.0f}) + HELOC limit "
                f"(${declared_heloc_limit:,.0f})"
                + (f" + secured line of credit limit (${declared_cf_limit:,.0f})"
                   if credit_facility_secured_here else "")
                + f" = ${combined_secured:,.0f} secured debt exceeds the charge "
                f"registered against {principal['id']!r} (${combined_limit:,.0f} = "
                f"{OSFI_B20_CHARGE_LTV_MAX:.0%} of ${house_value:,.0f} house value "
                f"-- OSFI B-20's legal maximum LTV for an uninsured combined loan "
                f"plan). Every facility secured against the SAME property shares "
                f"ONE registered charge -- this is not a valid combination (#664/#689)."
            )
        # Issue #1039: the OPENING POSITION cross-check (#664). The check
        # above validates the facilities' LIMITS -- potential borrowing.
        # This validates the household's TRUE OPENING POSITION: mortgage +
        # actually-drawn HELOC balance <= the registered charge. When the
        # drawn balance is within its own limit it is arithmetically implied
        # by the limits check above (drawn <= limit), so this fires only when
        # a document declares a drawn balance ABOVE its facility's limit that
        # also breaches the charge -- and it fires BEFORE the over-limit
        # refusal below so the household learns about the charge breach (the
        # fact that governs any refinancing) first, never just the
        # bookkeeping error.
        opening_drawn = heloc["balance"]["amount"] if heloc else 0
        if (opening_drawn > 0
                and declared_mortgage + opening_drawn > combined_limit + _CHARGE_TOLERANCE):
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares an opening "
                f"drawn balance of ${opening_drawn:,.0f}, which together with "
                f"the mortgage (${declared_mortgage:,.0f}) puts ${declared_mortgage + opening_drawn:,.0f} "
                f"of secured debt against {principal['id']!r} -- beyond the "
                f"charge registered on it (${combined_limit:,.0f} = "
                f"{OSFI_B20_CHARGE_LTV_MAX:.0%} of ${house_value:,.0f} house "
                f"value). An opening drawn position is honoured as a true "
                f"starting balance (#1039), but no true position can exceed "
                f"the registered charge securing it (#664). Refusing loudly "
                f"rather than simulating debt no lender could have advanced."
            )
        if heloc:
            revolving_limit = _heloc_revolving_limit(house_value, OSFI_B20_REVOLVING_LTV_MAX)
            if declared_heloc_limit > revolving_limit + _CHARGE_TOLERANCE:
                raise ContractAdaptationError(
                    f"Declared HELOC limit (${declared_heloc_limit:,.0f}) on "
                    f"{principal['id']!r} exceeds the revolving-only ceiling "
                    f"(${revolving_limit:,.0f} = {OSFI_B20_REVOLVING_LTV_MAX:.0%} of "
                    f"${house_value:,.0f} house value) -- OSFI B-20 caps the "
                    f"readvanceable/revolving segment of a combined loan plan at "
                    f"65% LTV independent of the 80% combined cap; lending between "
                    f"65% and 80% must be amortizing and non-readvanceable (#664). "
                    f"(A secured line_of_credit is NOT subject to this specific "
                    f"sub-ceiling -- it is not the mortgage-paired readvance "
                    f"mechanism OSFI's 65% figure targets -- only to the 80% "
                    f"combined cap above.)"
                )

    if heloc:
        # Issue #1039: an opening DRAWN balance is a TRUE STARTING POSITION,
        # not a refusal and not a silent drop. A household already partway
        # through a borrow-to-invest strategy starts the simulation from its
        # real position: heloc_balance = balance.amount, margin_available =
        # limit - drawn (less standby room), and a margin_tracing derived
        # from the DECLARED deductibility.investment_portion -- the original
        # borrowing's purpose is a historical fact that predates the
        # snapshot, so it is carried in, never re-derived from a simulation
        # decision (#577 governs DRAWS THE ENGINE MAKES; this honours a draw
        # THE HOUSEHOLD ALREADY MADE). DP#32 keeps absence loud: a declared
        # opening balance WITHOUT a deductibility block would leave the trace
        # un-derivable (defaulting it to 0 or 1 would both be fabrications),
        # so that combination still refuses.
        heloc_drawn = heloc["balance"]["amount"]
        heloc_deductibility = heloc.get("deductibility")
        if heloc_drawn > 0 and heloc_deductibility is None:
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares balance.amount="
                f"{heloc_drawn:,.0f} > 0, an OPENING DRAWN balance, but no "
                f"deductibility block. The engine honours an opening drawn "
                f"position as a true starting balance (#1039) -- but its "
                f"s.20(1)(c) trace cannot be derived: the original borrowing's "
                f"purpose is a historical fact only the document carries, and "
                f"defaulting it to fully-deductible or fully-personal would "
                f"both fabricate a tax position (DP#32). Declare "
                f"deductibility.investment_portion = p (the share of the "
                f"opening balance traced to investment use), or set "
                f"balance.amount = 0 if the facility is actually undrawn."
            )
        if heloc_drawn > heloc["limit"]:
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares "
                f"balance.amount={heloc_drawn:,.0f} above its own limit "
                f"({heloc['limit']:,.0f}). An opening drawn position is "
                f"honoured as a true starting balance (#1039), but a facility "
                f"cannot be drawn past its declared credit limit -- no lender "
                f"balances that. Fix the balance or the limit; refusing "
                f"loudly rather than simulating a negative undrawn room."
            )
        prop_cfg["margin_available"] = heloc["limit"] - heloc_drawn
        if heloc_drawn > 0:
            # Only written when a draw exists, so absence in the internal
            # dict keeps meaning "undrawn" -- the same >0-only convention
            # deductible_mortgage_balance uses (DP#24/DP#32).
            prop_cfg["heloc_opening_balance"] = heloc_drawn
            prop_cfg["heloc_opening_investment_portion"] = \
                heloc_deductibility["investment_portion"]
        prop_cfg["heloc_readvance"] = heloc.get("readvanceable", False)
        # issue #654: the HELOC's OWN declared rate/rate_type -- direct
        # indexing (not .get()), same as mortgage["rate"] above, because
        # the schema REQUIRES both on every kind=heloc liability, so their
        # absence here would mean validate_contract() was skipped, and
        # that must raise loudly (KeyError), never default. Mapped to
        # property.heloc_rate/.heloc_rate_type (SimulationConfig.heloc_rate/
        # .heloc_rate_type -> FamilySimulation's heloc_path), the ONLY
        # spelling the engine actually reads (#654 found and closed the
        # gap this comment used to describe: assumptions.rate_paths.heloc
        # -> assumptions_cfg["heloc_rate"] below was never read by
        # SimulationConfig.from_dict either -- "scenario_discovery.py's
        # real HELOC-rate consumer" was true only for scenario_discovery's
        # OWN strategy-discovery heuristics, never for the engine that
        # actually prices HELOC/SM interest, which is simulation.py's
        # build_heloc_path -- previously wired ONLY off property.
        # mortgage_rate, the #654/#595B bug).
        prop_cfg["heloc_rate"] = heloc["rate"]
        prop_cfg["heloc_rate_type"] = heloc["rate_type"]
        # Issue #1036: the three heloc declarations that used to be silently
        # dropped here are now each either READ or REFUSED LOUDLY (DP#32:
        # absence must fail loudly, never default to zero / never drop a
        # declared fact). The schema-coverage DEAD_ALLOWLIST entries for
        # these three are removed in the same PR -- a leaf is no longer
        # allowlisted as dead once the engine consumes or refuses it.
        #
        # 1. capitalize_interest -- READ. Mapped to property.capitalize_interest
        #    and consumed by simulation_rules.apply_margin_heloc_interest: false
        #    = service the drawn-margin interest in CASH (via the existing
        #    heloc_interest_servicing rule); true = capitalize up to the charge,
        #    servicing the rest (the pre-#1036 behaviour). A retiree paying
        #    HELOC interest in cash is no longer modelled as capitalizing it.
        #    The internal-config default (when this key is absent, e.g. every
        #    test that builds the internal dict directly) is True -- byte-
        #    identical to the pre-#1036 capitalization path (DP#32: absence is
        #    the fallback, never a coercion of a supplied value).
        prop_cfg["capitalize_interest"] = heloc["capitalize_interest"]
        # 2. balance (the DRAWN amount) -- HONOURED as the opening position
        #    when > 0 (issue #1039): mapped to property.heloc_opening_balance
        #    (+ property.heloc_opening_investment_portion off the declared
        #    deductibility), read by SimulationConfig.from_dict, seeded into
        #    SimState.heloc_balance / canada.margin_tracing by SimState.initial
        #    -- wired all the way into the engine (DP#18). See the block at
        #    the top of this `if heloc:` for the refusals that keep absence
        #    loud (a draw with no deductibility; a draw above the limit).
        #    balance = 0 (undrawn) remains the documented accepted state
        #    (#577): margin_available then equals the full limit.
        # 3. deductibility -- REFUSED LOUDLY when declared WITHOUT an opening
        #    drawn balance and asserting a deductible portion (> 0). With an
        #    opening draw it IS honoured (the #1039 opening trace); with none,
        #    there is no opening interest to apply it to and future draws are
        #    traced from their borrowing's purpose -- silently dropping the
        #    declaration would be exactly the DP#32 defect, so it refuses
        #    (same stance as the consumer-loan path at the consumer-loan
        #    mapping, whose message instructs 'declare investment_portion=0').
        #    investment_portion=0 is accepted either way -- the safe, accepted
        #    state the user's real contracts declare.
        if (heloc_drawn <= 0 and heloc_deductibility is not None
                and heloc_deductibility["investment_portion"] > 0):
            raise ContractAdaptationError(
                f"liability {heloc['id']!r} (kind=heloc) declares deductibility."
                f"investment_portion={heloc_deductibility['investment_portion']:.4f} "
                f"> 0 while balance.amount is 0 (undrawn). A declared ratio is "
                f"honoured only as the trace of an OPENING drawn balance "
                f"(#1039); with nothing drawn there is no interest to apply it "
                f"to, and future draws are traced from their borrowing's "
                f"purpose -- silently dropping this declaration would be the "
                f"DP#32 defect. Declare investment_portion=0 (personal-use, "
                f"not deductible), declare the real balance.amount with this "
                f"deductibility to honour an opening position, or express a "
                f"draw the engine should make via decisions.borrow_to_invest "
                f"(#1036). Refusing loudly."
            )

    if credit_facility:
        # issue #689: makes the facility reachable by the engine at all --
        # SimulationConfig.credit_facility_limit/_rate/_rate_type/_secured,
        # consumed by simulation_rules.apply_solvency (issue #679's
        # liquidation waterfall's second rung) and by
        # trajectory_invariants.check_total_secured_debt_within_charge (only
        # when secured). The opening drawn balance is deliberately NOT
        # mapped -- same reasoning as heloc.balance above (#577): whether
        # this facility is ever drawn is a simulation decision (only the
        # #679 waterfall draws it, in a shortfall year), never a fact read
        # off this field. An undrawn facility therefore starts, and stays,
        # undrawn until a real shortfall reaches it.
        prop_cfg["credit_facility_limit"] = credit_facility["limit"]
        prop_cfg["credit_facility_rate"] = credit_facility["rate"]
        prop_cfg["credit_facility_rate_type"] = credit_facility["rate_type"]
        prop_cfg["credit_facility_secured"] = credit_facility_secured_here

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
        from simulation_config import apply_structure_overlay, ChargeLimitExceededError
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

    resp_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "resp"]
    resp_balance = sum(a["balance"]["amount"] for a in resp_accounts)
    accounts_cfg: Dict[str, Any] = {"resp_current_balance": resp_balance}
    if resp_accounts:
        # #647: SUM every RESP account's composition into the one household
        # bucket the engine tracks (SimState has a single resp_contributions/
        # _cesg/_qesi total, split evenly across children -- a #601/#643
        # follow-up, not this PR's job) -- taking only accounts[0] silently
        # dropped every subsequent RESP's contribution/grant history (a real
        # dollar amount, not a rounding nicety: a family with a joint RESP
        # plus a grandparent-funded second RESP lost the second entirely).
        total_contrib = sum(a["resp"]["contributions_total"] for a in resp_accounts)
        total_cesg = sum(a["resp"]["cesg_received"] for a in resp_accounts)
        total_qesi = sum(a["resp"]["qesi_received"] for a in resp_accounts)
        accounts_cfg["resp_composition"] = {
            "total_contributions": total_contrib,
            "total_cesg_received": total_cesg,
            "total_qesi_received": total_qesi,
            "investment_earnings": max(0.0, resp_balance - total_contrib - total_cesg - total_qesi),
        }

    lira_accounts = [a for a in doc.get("accounts", []) if a["kind"] in ("lira", "lif")]
    lira_cfg: Dict[str, Any] = {}
    if lira_accounts:
        # #647: the engine tracks exactly ONE lira/lif pot (household-
        # singleton, #643). A second LIRA/LIF's BALANCE is only safe to sum
        # into that one pot if every account agrees on the facts that
        # SELECT which withdrawal-limit rule applies -- owner birth year
        # (the age-based limit table), jurisdiction, and reference_rate.
        # Silently taking accounts[0] and dropping a second, disagreeing
        # LIRA is exactly #647's bug; silently summing balances under
        # DIFFERENT rules would apply the wrong limit to part of the money.
        # Refuse rather than guess.
        first = lira_accounts[0]
        first_bd = _people_by_id(doc)[first["owner"]].get("birth_date")
        first_birth_year = int(first_bd[:4]) if first_bd else None
        # Issue #708: an elected early-conversion date (lira.conversion_date).
        # Null/absent = no early election; the age-71 mandatory backstop then
        # applies (unchanged behaviour). Two LIRA/LIF accounts must agree on
        # the election (the engine tracks one pot, #643) — refuse rather than
        # silently pick one.
        first_conversion_date = first["lira"].get("conversion_date")
        for acc in lira_accounts[1:]:
            acc_bd = _people_by_id(doc)[acc["owner"]].get("birth_date")
            acc_birth_year = int(acc_bd[:4]) if acc_bd else None
            if (acc_birth_year != first_birth_year
                    or acc["lira"]["jurisdiction"] != first["lira"]["jurisdiction"]
                    or acc["lira"]["reference_rate"] != first["lira"]["reference_rate"]
                    or acc["lira"].get("conversion_date") != first_conversion_date):
                raise ContractAdaptationError(
                    f"Accounts {first['id']!r} and {acc['id']!r} are both "
                    f"kind=lira/lif, but disagree on owner birth year, "
                    f"jurisdiction, reference rate, or conversion date. The "
                    f"engine tracks exactly one LIRA/LIF pot (#643) -- "
                    f"blending two accounts whose withdrawal-limit rules or "
                    f"conversion election genuinely differ would silently "
                    f"apply the wrong rule to part of the money. Cannot "
                    f"represent both."
                )
        # conversion_date is a full date (DP#1); the conversion fires on a
        # calendar-year boundary, so derive the election year from it.
        conversion_year = (int(first_conversion_date[:4])
                           if first_conversion_date else None)
        lira_cfg = {
            "balance": sum(a["balance"]["amount"] for a in lira_accounts),
            "birth_year": first_birth_year,
            "jurisdiction": first["lira"]["jurisdiction"],
            "reference_rate": first["lira"]["reference_rate"],
            # Issue #708: the elected early-conversion calendar year (or
            # None for no early election -> age-71 backstop). Read by
            # simulation_state's canada-state build -> opening_lira_
            # conversion_year -> apply_lira_lif.
            "conversion_year": conversion_year,
            # source_pension_plan/transfer_date are NOT mapped (epic #603
            # Phase 2b finding): lira.source_pension_plan/.transfer_date were
            # already confirmed dead and DELETED from the legacy schema in
            # Phase 2a (zero production readers -- simulation_state.py's
            # lira_cfg handling reads only .balance/.birth_year/.jurisdiction/
            # .reference_rate/.conversion_year). Mapping them here would
            # recreate exactly the "written, not applied" duplicate
            # declaration Phase 2a deleted.
        }

    # #649: an LSIF is either a STANDALONE kind=lsif account, OR held INSIDE
    # an RRSP wrapper -- the Quebec 'REER FTQ', a kind=rrsp/spousal_rrsp
    # account carrying a nested `lsif` sub-object. The two are taxed
    # differently: the wrapper (RRSP) supplies the deduction/deferral/RRIF
    # base, which the RRSP balance mapping above ALREADY applies; the nested
    # lsif leaf adds ONLY the 30% LSIF credit, computed on the declared
    # `lsif.holding_amount` (the LSIF portion of that RRSP -- NOT the whole
    # balance, which also holds other RRSP assets). A standalone lsif account
    # has no wrapper, so its credit is computed on its own balance (unchanged).
    lsif_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "lsif"]
    nested_lsif_accounts = [
        a for a in doc.get("accounts", [])
        if a["kind"] in ("rrsp", "spousal_rrsp") and a.get("lsif") is not None
    ]
    lsif_bearing = lsif_accounts + nested_lsif_accounts
    lsif_cfg: Dict[str, Any] = {}
    if len(lsif_bearing) > 1:
        # #647: each LSIF purchase carries its OWN eligibility facts
        # (purchase date, province, prior redemption, HBP-replacement
        # status) that drive the tax-credit calculation
        # (countries/canada/lsif_credit.py) for that specific purchase --
        # unlike a plain balance, these cannot be blended into one "average"
        # purchase without silently misapplying one purchase's eligibility
        # to another's money. Taking accounts[0] and dropping the rest (the
        # previous behaviour) silently lost real dollars; refuse instead.
        # #649: standalone lsif accounts AND nested-in-RRSP lsif holdings are
        # counted together -- the engine still represents exactly one purchase.
        raise ContractAdaptationError(
            f"Document declares {len(lsif_bearing)} LSIF holdings "
            f"({sorted(a['id'] for a in lsif_bearing)}, standalone kind=lsif "
            f"accounts and/or lsif nested in an RRSP wrapper -- #649). The "
            f"engine's LSIF model (countries/canada/lsif_credit.py) represents "
            f"exactly ONE purchase -- its own purchase date/amount/province "
            f"drive the credit calculation. Blending multiple purchases into "
            f"one would silently apply one purchase's eligibility to the "
            f"other's money (#643). Cannot represent more than one."
        )
    if lsif_bearing:
        acc = lsif_bearing[0]
        if acc["kind"] == "lsif":
            owner = _people_by_id(doc)[acc["owner"]]
            purchase_amount = acc["balance"]["amount"]
        else:
            # #649 nested-in-RRSP (the 'REER FTQ'): the credit is on the
            # declared LSIF portion, which the wrapper's balance cannot supply.
            holding_amount = acc["lsif"].get("holding_amount")
            if holding_amount is None:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={acc['kind']}) declares a "
                    f"nested `lsif` block (an LSIF held inside the RRSP wrapper "
                    f"-- the 'REER FTQ', #649) but no `lsif.holding_amount`. "
                    f"The 30% LSIF credit is computed on the LSIF PORTION of "
                    f"the wrapper, which is not the whole RRSP balance and must "
                    f"be declared, not guessed (DP#32)."
                )
            owner_id = next(iter(_owner_shares(acc["owner"])), None)
            owner = _people_by_id(doc)[owner_id]
            purchase_amount = holding_amount
        lsif_cfg = {
            "purchase_amount": purchase_amount,
            "purchase_year": int(acc["lsif"]["purchase_date"][:4]),
            "is_quebec_resident": acc["lsif"]["purchase_province"] == "quebec",
            "prior_redemption": acc["lsif"]["prior_redemption"],
            "employment_income": _active_employment_income(owner, as_of),
            "reference_year_taxable_income": acc["lsif"].get("reference_year_taxable_income"),
            "quebec_carryforward": acc["lsif"]["quebec_carryforward"],
            "is_hbp_replacement": acc["lsif"]["is_hbp_replacement"],
            "federally_registered": acc["lsif"]["federally_registered"],
            "acquisition_date": acc["lsif"].get("acquisition_date"),
            "redeemed_date": acc["lsif"].get("redeemed_date"),
        }

    # #599 follow-up (Phase 2b): non_reg is still a HOUSEHOLD singleton in the
    # internal shape (SimState carries one non_reg_balance/non_reg_acb, not
    # one per owner) -- multiple non_reg accounts (e.g. one per spouse) are
    # summed into that one household total, the same "owner-summed" treatment
    # _map_member already gives rrsp/tfsa. cost_basis sums only the accounts
    # that DECLARE an acb; if every non_reg account has acb=null (unknown),
    # the household cost_basis is None too -- an unknown mixed with a known
    # number is not a number (DP#32: don't fabricate precision that isn't
    # there). Holdings are combined from every account (weights are
    # per-holding fractions of THAT account's balance; blending across
    # accounts as one combined holdings list is the existing PortfolioConfig
    # shape's only representation of "the household's non-reg composition").
    non_reg_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "non_reg"]
    portfolio_cfg: Dict[str, Any] = {}
    if non_reg_accounts:
        declared_acbs = [a["acb"] for a in non_reg_accounts if a.get("acb") is not None]
        portfolio_cfg["accounts"] = {
            "non_reg": {
                "balance": sum(a["balance"]["amount"] for a in non_reg_accounts),
                "cost_basis": sum(declared_acbs) if len(declared_acbs) == len(non_reg_accounts) else None,
                "holdings": [
                    {"product": h["product"], "weight": h["weight"]}
                    for acc in non_reg_accounts for h in acc.get("holdings", [])
                ],
            }
        }
    products = doc["assumptions"]["products"]  # both schema-required (may be {})
    if products:
        portfolio_cfg["products"] = products

    # Issue #917: thread each REGISTERED pot's declared product holdings into
    # portfolio.accounts.{rrsp,tfsa} as a derived composition + yield -- the
    # shape #641's WHT drag (PortfolioConfig.registered_wht_drag) and #473's
    # asset-location optimizer both read. Before this, only non_reg composition
    # crossed this boundary, so a --input contract declaring "US equity in my
    # RRSP" got the flat rate and no placement advice (both no-ops). Unlike
    # non_reg (a household singleton read only through its own after-tax path,
    # so a raw holdings passthrough suffices), the rrsp/tfsa optimizer reads
    # composition + yield off the config verbatim -- so the product->composition
    # derivation has to happen HERE. That derivation is the jurisdiction's
    # product model; reusing PortfolioConfig.from_dict/to_dict (one derivation,
    # DP#9) via the same lazy countries.canada import this adapter already uses
    # for fonds_ftq keeps a single WHT/composition model rather than a second
    # spelling at the boundary. Registered pots that declare no holdings add no
    # entry at all -- a strict no-op (DP#32), byte-identical to today.
    registered_accounts = _registered_composition_accounts(doc, products)
    if registered_accounts:
        portfolio_cfg.setdefault("accounts", {}).update(registered_accounts)

    assumptions = doc["assumptions"]
    # default_non_reg_yield/capital_gains_inclusion are explicitly nullable
    # beliefs (null = "no coarse fallback"/"use the code table"); a genuine
    # 0.0 must NOT be coerced to the legacy default (DP#32) -- `is None`,
    # never `or`.
    default_yield = assumptions["default_non_reg_yield"]
    cg_inclusion = assumptions["tax_law_overrides"]["capital_gains_inclusion"]
    assumptions_cfg: Dict[str, Any] = {
        "inflation": assumptions["inflation"],
        "salary_growth": assumptions["salary_growth"],
        "start_year": start_year,
        "time_step": assumptions["time_step"],
        "non_reg_yield_rate": 0.02 if default_yield is None else default_yield,
        "capital_gains_inclusion": 0.50 if cg_inclusion is None else cg_inclusion,
        "frozen_brackets": assumptions["tax_law_overrides"]["frozen_brackets"],
        # #585 (epic #603 Phase 2b finding): the document's own units --
        # root-level currency/dollars/real_base_year -- had no path into the
        # internal shape at all, so model_fidelity.describe_units (which
        # reads assumptions.currency/.dollar_basis/.base_year to declare
        # nominal-vs-real on every output surface) silently fell back to
        # its defaults for every contract-sourced run. Mapped for real here.
        "currency": doc["currency"],
        "dollar_basis": doc["dollars"],
    }
    if doc["dollars"] == "real":
        assumptions_cfg["base_year"] = doc["real_base_year"]
    resp_beliefs = assumptions.get("resp")
    if resp_beliefs:
        assumptions_cfg["resp_eap_tax_rate"] = resp_beliefs["eap_tax_rate"]
        assumptions_cfg["resp_eap_taxable_portion"] = resp_beliefs["eap_taxable_portion"]
        # issue #578 (epic #603 Phase 2b): these three used to have NO home
        # anywhere in the contract schema at all -- worse than dead, they
        # were unrepresentable, so a contract document could never override
        # SimulationConfig's defaults (study_start_age=18/duration=4/
        # used_for_education=True). Real config fields, mapped into
        # `accounts` (SimulationConfig.from_dict reads them off
        # `accounts.resp_study_start_age` etc., not `assumptions.*`).
        accounts_cfg["resp_study_start_age"] = resp_beliefs["study_start_age"]
        accounts_cfg["resp_study_duration_years"] = resp_beliefs["study_duration_years"]
        accounts_cfg["resp_used_for_education"] = resp_beliefs["used_for_education"]

    # Issue #993 (DP#21): the household's sleeve return beliefs pass through
    # VERBATIM into the internal shape, where risk_allocation's
    # `_resolve_return_beliefs` reads them to price the recommended mix's
    # MC risk metrics. Not flattened key-by-key here: a PARTIAL declaration
    # is legitimate input -- risk_allocation merges it over its documented
    # defaults, so flattening (or defaulting missing keys) HERE would
    # duplicate that merge and freeze this file's spelling of the defaults
    # (DP#9: one spelling of each belief). Absent block = absent key; an
    # explicit 0.0 survives (`is not None`, never `or` -- DP#32).
    return_beliefs = assumptions.get("return_beliefs")
    if return_beliefs is not None:
        assumptions_cfg["return_beliefs"] = dict(return_beliefs)

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

    horizon = doc["decisions"]["horizon"]  # schema-required
    if horizon["person"] == primary_id:
        assumptions_cfg["horizon_age"] = horizon["until_age"]

    # ── issue #685: the belief does NOT get to overwrite the signed rate ──
    #
    # This block used to read
    #
    #     heloc_path = assumptions["rate_paths"]["heloc"]
    #     if heloc_path["type"] == "fixed":
    #         assumptions_cfg["heloc_rate"] = heloc_path["rate"]
    #
    # and that single line is #685. `assumptions.heloc_rate` is the
    # DECISION channel — `resolve_heloc_rate`'s tier 1, written by
    # `optimize.apply_anchor_preset` for a deliberate, labelled hypothetical
    # ("what if the HELOC also reprices on renewal", DP#5) — and it outranks
    # `property.heloc_rate`, the household's OWN signed rate (#654's
    # canonical spelling), precisely so that such a decision is not shadowed
    # by it. Piping a rate_paths BELIEF through that same channel handed the
    # belief the decision's authority: a stale rate_paths block silently
    # outranked a signed contract, and no output said so.
    #
    # So the contract loader no longer writes the decision channel at all.
    # The signed rate reaches the engine and every optimizer/report consumer
    # by the one spelling that is a fact — property.heloc_rate, mapped from
    # liabilities[kind=heloc].rate above. `rate_paths` keeps its real job
    # (what the borrowing costs AFTER the current term; there is no
    # year-over-year rate PATH in the engine to consume that yet) and gains
    # a real one it never had: it is now RECONCILED against the contract,
    # and a disagreement about year zero is reported instead of silently won.
    rate_conflicts = _reconcile_rate_paths(
        assumptions["rate_paths"], {"mortgage": mortgage, "heloc": heloc})
    if rate_conflicts:
        # Read back by model_fidelity.rate_path_conflicts() so the JSON/HTML/
        # console reports name the same figures this warning does -- a warning
        # scrolls off; the report does not.
        assumptions_cfg["rate_path_conflicts"] = rate_conflicts
        for c in rate_conflicts:
            logger.warning(
                "CONTRADICTION (#685): assumptions.rate_paths.%s asserts %.2f%% for "
                "the CURRENT year, but liability %r (kind=%s) declares a SIGNED rate "
                "of %.2f%%. A signed rate is a FACT; a rate path is a BELIEF -- the "
                "DECLARED %.2f%% WINS and is what this run charges. rate_paths "
                "describes what this borrowing costs AFTER the current term ends; it "
                "cannot reprice a rate the contract has already pinned. Fix the "
                "document: set rate_paths.%s to the signed rate (or drop it) unless "
                "you meant to state a renewal belief -- in which case it disagrees "
                "with your own contract at year zero.",
                c["liability_kind"], c["believed_rate"] * 100, c["liability_id"],
                c["liability_kind"], c["declared_rate"] * 100, c["declared_rate"] * 100,
                c["liability_kind"],
            )

    oas_override = assumptions["tax_law_overrides"].get("oas")
    retirement_cfg: Dict[str, Any] = assumptions["retirement"]  # schema-required
    retirement_out: Dict[str, Any] = {}
    if retirement_cfg.get("drawdown_order"):
        retirement_out["drawdown_order"] = retirement_cfg["drawdown_order"]
    if retirement_cfg.get("spending_target") is not None:
        retirement_out["spending_target"] = retirement_cfg["spending_target"]
    if retirement_cfg.get("net_replacement_rate") is not None:
        # epic #603 Phase 2b finding: real, consumed field (simulation.py's
        # ret.get('net_replacement_rate', DEFAULT_NET_REPLACEMENT_RATE))
        # that this mapping was silently dropping -- schema-declared,
        # engine-read, but never reached. Wired for real here.
        retirement_out["net_replacement_rate"] = retirement_cfg["net_replacement_rate"]
    # Issue #1009: the opt-in die-with-(near)-zero drawdown mode
    # (simulation_rules.apply_retirement_income reads
    # ret.get('liquidate_to_target')). Absence-safe: carried only when the
    # contract declares it (a bool leaf -- absent == off, never coerced), so a
    # household that does not opt in is byte-identical (DP#32).
    if retirement_cfg.get("liquidate_to_target") is not None:
        retirement_out["liquidate_to_target"] = bool(
            retirement_cfg["liquidate_to_target"])
    # retirement_cfg["drawdown_tax_mode"] is NOT mapped: issue #579 deleted
    # the gross-drawdown code path entirely (the 'gross' vs 'net' switch has
    # zero readers anywhere in the engine today, confirmed by grep) -- the
    # schema keeps the field for its own documentation value ("gross exists
    # only for back-testing"), but there is nothing left for a mapped value
    # to switch.
    if oas_override:
        if oas_override.get("disabled"):
            logger.warning(
                "assumptions.tax_law_overrides.oas.disabled=true has NO legacy "
                "equivalent (issue #592 -- the legacy engine cannot represent "
                "'no OAS' other than an annual_max_override of 0, which is "
                "exactly #592's bug). Mapping to oas_annual_max=0 reproduces "
                "the known gap rather than truly disabling OAS; fixing this "
                "requires #592 on the engine side, out of Phase 1's scope."
            )
            retirement_out["oas_annual_max"] = 0
        elif oas_override.get("annual_max_override") is not None:
            retirement_out["oas_annual_max"] = oas_override["annual_max_override"]

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

    resp_action_scenarios = [
        {"id": a["id"], "label": a["label"]}
        for a in doc["decisions"]["resp_action"]  # schema-required (may be [])
    ]

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
    if declared_targets:
        assumptions_cfg["deduct_later_bracket_target"] = declared_targets.pop()

    estate_cfg = _map_estate(doc, primary_id, spouse_id)

    # Issue #679: household_budget is optional (DP#16 -- see the schema's
    # own description) and its leaf is nullable, so it is mapped only when
    # actually measured (explicit presence test, never a truthiness/`or`
    # coercion -- DP#32).
    household_budget_cfg = doc.get("household_budget")
    household_budget_out: Dict[str, Any] = {}
    if household_budget_cfg:
        if household_budget_cfg.get("annual_living_costs") is not None:
            household_budget_out["living_costs"] = household_budget_cfg["annual_living_costs"]
        # Issue #761: the discretionary/non-discretionary split of the
        # measured living-cost scalar. Optional (DP#16): absent reproduces
        # today's behaviour exactly (the whole scalar is rigid). Explicit
        # presence test, never a truthiness/`or` coercion -- 0.0 is a real,
        # declarable answer ("all my spending is non-discretionary"),
        # distinct from omitting the field (DP#32).
        if household_budget_cfg.get("discretionary_fraction") is not None:
            # DP#32: a discretionary fraction of NOTHING is a contradiction --
            # the fraction is a share OF annual_living_costs, so declaring it
            # without a measured scalar must fail loudly, never default the
            # missing scalar to zero (which would silently make the split a
            # no-op) or to the full amount. The two halves of the split are
            # not independently optional.
            if household_budget_cfg.get("annual_living_costs") is None:
                raise ContractAdaptationError(
                    f"household_budget.discretionary_fraction = "
                    f"{household_budget_cfg['discretionary_fraction']!r} is "
                    f"declared, but household_budget.annual_living_costs is "
                    f"not measured. The discretionary fraction is a SHARE OF the "
                    f"annual living-cost scalar -- declaring one without the "
                    f"other is a contradiction, not a default-to-zero (DP#32). "
                    f"Measure annual_living_costs (12 months of bank/credit "
                    f"statements) before declaring how much of it is "
                    f"discretionary, or omit discretionary_fraction to keep the "
                    f"whole scalar rigid (issue #761)."
                )
            household_budget_out["discretionary_fraction"] = (
                household_budget_cfg["discretionary_fraction"])

        # Issue #760: dated, finite-term living-cost segments layered on top of
        # the perpetual annual_living_costs scalar (a private-school tuition
        # that ENDS when a child ages out, childcare, a term expense that
        # stops). Optional (DP#16): absent reproduces today's behaviour exactly
        # (the golden invariant does not move, DP#32). Mapped into the internal
        # household_budget shape the fold reads
        # (simulation_rules.apply_solvency, via SimulationConfig.expense_segments)
        # so the segments reach a decision, not sit as dead leaves (DP#18).
        segments_in = household_budget_cfg.get("expense_segments")
        if segments_in:
            # DP#32: a dated living-cost segment is a share ON TOP OF the
            # measured base scalar -- declaring one without annual_living_costs
            # must fail loudly (the solvency module is engaged by the base;
            # DP#16), never default the missing base to zero. Same non-
            # independence as the discretionary split above.
            if household_budget_cfg.get("annual_living_costs") is None:
                raise ContractAdaptationError(
                    f"household_budget.expense_segments is declared "
                    f"({len(segments_in)} segment(s)), but household_budget."
                    f"annual_living_costs is not measured. A dated expense "
                    f"segment is an ADDITIONAL living cost layered on the "
                    f"measured base scalar -- declaring one without the base is "
                    f"a contradiction, not a default-to-zero (DP#32). Measure "
                    f"annual_living_costs (12 months of bank/credit statements) "
                    f"before declaring dated segments on top of it (issue #760)."
                )
            expense_segments: List[Dict[str, Any]] = []
            for seg in segments_in:
                # DP#32: a zero/negative window is not silently treated as $0 --
                # it is refused. `to` is nullable (null = perpetual, the explicit
                # spelling), but a NON-null `to` on or before `from` is a
                # contradiction (an expense that ends before it starts).
                if seg["to"] is not None and seg["to"] <= seg["from"]:
                    raise ContractAdaptationError(
                        f"household_budget expense_segment "
                        f"{seg['description']!r} declares to={seg['to']!r} on or "
                        f"before from={seg['from']!r} -- an expense that ends "
                        f"before it starts is an empty window. Declare to strictly "
                        f"after from, or to: null for a perpetual segment "
                        f"(issue #760); silently treating it as $0 is the DP#32 "
                        f"trap."
                    )
                # Issue #882: OPTIONAL intra-year seasonality. `active_months` (a
                # subset of 1..12) spends the ANNUAL `amount` in equal shares
                # across only those months (heating Nov-Mar, a property-tax bill
                # each July); absence means active every day of the window
                # (byte-for-byte #760, DP#32). Its structural constraints -- a
                # non-empty list of unique months in 1..12 -- are enforced
                # declaratively by the schema (minItems/uniqueItems/range), which
                # to_internal_config runs before this loop, so an empty or
                # duplicated list is already refused loudly upstream (DP#32); no
                # redundant guard here (DP#9).
                active_months = seg.get("active_months")
                mapped = {
                    "description": seg["description"],
                    "amount": seg["amount"],
                    # Dates travel through as ISO strings; the fold rule parses
                    # them once per year (simulation_rules._expense_segment_
                    # contribution_in_year), the same string->date convention
                    # apply_installments uses for start_date.
                    "from": seg["from"],
                    "to": seg["to"],
                    "non_discretionary": seg["non_discretionary"],
                }
                # Only carry the key when declared -- an absent active_months
                # round-trips to absent, never a fabricated all-months block
                # (DP#24/DP#32).
                if active_months is not None:
                    mapped["active_months"] = active_months
                expense_segments.append(mapped)
            household_budget_out["expense_segments"] = expense_segments

    # Issue #688: the emergency-reserve POLICY. `held_in` names an ACCOUNT ID,
    # but this engine tracks one pot per account KIND (#643), so the id is
    # resolved to its kind here -- the mapping layer is exactly where a
    # document-level reference becomes an engine-level one. An id that names
    # no declared account is refused rather than silently dropped: a reserve
    # pointed at an account that does not exist is not a reserve of zero, it
    # is a typo, and the two must never be confused (DP#32).
    reserve_cfg = assumptions.get("emergency_reserve")
    reserve_out: Dict[str, Any] = {}
    if reserve_cfg:
        held_in_id = reserve_cfg.get("held_in")
        held_in_kind = None
        if held_in_id is not None:
            hosts = [a for a in doc.get("accounts", []) if a["id"] == held_in_id]
            if not hosts:
                raise ContractAdaptationError(
                    f"assumptions.emergency_reserve.held_in = {held_in_id!r}, but no "
                    f"account with that id is declared. The reserve is a cash sleeve "
                    f"carved out of a real account (#688) -- it cannot be held in an "
                    f"account that does not exist. Declared account ids: "
                    f"{sorted(a['id'] for a in doc.get('accounts', []))}."
                )
            held_in_kind = hosts[0]["kind"]
            # #643: the engine holds ONE pot per kind, and the primary's TFSA
            # and the spouse's are separate pots. Attribute a TFSA reserve to
            # whichever spouse's pot actually owns the account.
            if held_in_kind == "tfsa" and hosts[0].get("owner") == spouse_id:
                held_in_kind = "tfsa_spouse"
        reserve_out = {
            "target_months": reserve_cfg["target_months"],   # schema-required
            "rate": reserve_cfg["rate"],                     # schema-required
            "instrument": reserve_cfg["instrument"],         # schema-required
            "held_in": held_in_kind,
        }

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
        origination_flow = {
            "year": start_year,
            "amount": origination_cash_back,
            "tax_treatment": "non-taxable",
        }
        # Issue #1075 (optimizer half): a cash-back that declares
        # ``min_house_amount`` is CONDITIONAL on the swept house tranche
        # amount -- the key rides ON the flow, so the sweep's cell
        # composition (optimize.py) withholds the inflow for a sweep point
        # whose house amount is below it, and a household that never
        # declares the condition keeps today's unconditional credit
        # byte-for-byte (DP#13/DP#32: the key's absence is the marker, and
        # no other cash_flow can carry it -- the schema forbids the key on
        # user-declared flows, so the adapter's flow is the only one gated).
        min_house = mortgage.get("cash_back_min_house_amount") if mortgage else None
        if min_house is not None:
            origination_flow["min_house_amount"] = min_house
        legacy_cash_flows.append(origination_flow)

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
        "tax": {"country": doc["jurisdiction"]["country"], "province": doc["jurisdiction"]["province"]},
        "cash_flows": legacy_cash_flows,
    }
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
            # Phase 2c fix (see the decisions.mortgage comment above): a
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

    # Issue #1036: decisions.borrow_to_invest -- a first-class, swept,
    # objective-ranked one-shot borrow-to-invest decision (draw $X against a
    # declared HELOC at year 0, invest the proceeds in non-reg, deduct the
    # interest under ITA s.20(1)(c)). Mirrors decisions.mortgage
    # .structure_options[] / refinance_options[]: each declared option is a
    # rung on the amount ladder, and the optimizer also runs the implicit
    # amount=0 (no-draw) baseline so 'do nothing' is always the frame of
    # reference (DP#33: a declaration is a lens, not a blindfold -- the sweep
    # is not replaced by the declared set, it is annotated by it). The draw
    # reuses the year-0 margin-draw machinery (initial_state_for_run); the
    # interest is priced and deducted by the existing drawn-margin rules,
    # traced 100% investment. No readvanceable facility is required -- a
    # mortgage-free household with a HELOC can express leverage this way.
    #
    # DP#32 boundary refusals (a partial/unsupported declaration must FAIL
    # LOUDLY, never silently coerce to the supported value):
    #   - `source` must resolve to a declared kind=heloc liability. A typo or a
    #     reference to a non-heloc liability (a margin account, an unsecured
    #     line_of_credit) is refused loudly -- the engine draws against a
    #     property-secured HELOC in this slice; other sources are follow-up.
    #   - `amount` must be > 0 and <= the source HELOC's limit. A draw larger
    #     than the limit is refused (it would silently cap, hiding the
    #     over-limit declaration); a zero/negative amount is refused (it is
    #     not a borrow-to-invest candidate, it is the no-draw baseline the
    #     sweep already runs implicitly).
    #   - `target_account` must be `non_reg`. Registered targets (RRSP/TFSA)
    #     are non-deductible under s.18(11) and are refused loudly until a
    #     separate slice models them.
    # Mapped only when the household declares some: an empty list is a
    # household with no borrow-to-invest question (the golden path), and
    # emitting it would move the internal shape away from 'absent' -- kept
    # conditional so a no-borrow-to-invest household round-trips byte-
    # identically, matching every other optional block (DP#24/DP#32). The
    # unwind trigger (#1017 decumulation lever) is NOT modelled in this slice.
    borrow_to_invest = list(doc["decisions"].get("borrow_to_invest", []))
    if borrow_to_invest:

        # The set of declared kind=heloc liability ids + their limits, for
        # source resolution and the amount<=limit check. The engine supports a
        # single HELOC facility today (_find_liability returns one), but the
        # schema permits more than one, so resolve by id across all of them.
        heloc_limits = {
            liab["id"]: liab["limit"]
            for liab in doc.get("liabilities", [])
            if liab["kind"] == "heloc"
        }
        # The engine models ONE HELOC facility today: `heloc` (found above by
        # _find_liability, scoped to the principal residence then any heloc) is
        # that facility, and `property.margin_available` is ITS limit. The
        # single-facility charge check refuses two helocs on the PRINCIPAL
        # residence, but a SECOND heloc on a non-principal (recreational/rental)
        # property passes it -- so `heloc_limits` can hold more than one entry
        # (D5). A borrow-to-invest source must be the FACILITY heloc (`heloc`):
        # the draw is booked on `new_heloc_balance` (the engine's one drawn
        # margin), at the facility's rate and charge room. A source naming any
        # other heloc (e.g. a recreational-property line) would pass the
        # amount<=limit check against THAT line's limit while the draw actually
        # hits the principal facility -- silently aliasing another liability,
        # exactly the DP#32 defect. Refuse unless source == the facility.
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
    if installment_plans:
        legacy["installments"] = installment_plans

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
    if equity_grants:
        legacy["equity_grants"] = equity_grants

    presets = doc["sensitivity"]["presets"]  # both schema-required (may be {})
    if presets:
        legacy["sensitivity_overlay_presets"] = presets

    # Issue #766: the contract's two spending figures -- the MEASURED
    # working-phase `household_budget.annual_living_costs` and the retirement
    # `assumptions.retirement.spending_target` the decumulation sizes to --
    # are consumed by different subsystems and never compared. A guessed
    # retirement target can silently outrank a measured living-cost figure and
    # produce a spurious decumulation shortfall. Reconcile them here (the only
    # place both are visible at once) and record the disagreement so
    # model_fidelity.spending_figure_conflicts() can name both figures on every
    # output surface that prints a decumulation number. Mirrors #685's
    # _reconcile_rate_paths. The band is a heuristic, not a requirement that
    # they be equal -- a retirement target legitimately differs from working-
    # life spend; a MATERIAL gap is the defect.
    spending_conflicts = _reconcile_spending_figures(
        doc, household_budget_out.get("living_costs"),
        retirement_out.get("spending_target"))
    if spending_conflicts:
        assumptions_cfg["spending_figure_conflicts"] = spending_conflicts
        for c in spending_conflicts:
            lc = c["living_costs_confidence"]
            lc_conf = "(no provenance entry)" if lc is None else lc
            st = c["spending_target_confidence"]
            st_conf = "(no provenance entry)" if st is None else st
            logger.warning(
                "CONTRADICTION (#766): the contract declares two spending figures "
                "that disagree by a factor of %.2fx: household_budget."
                "annual_living_costs = %.0f (provenance: %s) vs. assumptions."
                "retirement.spending_target = %.0f (provenance: %s). They are not "
                "the same quantity (working-life vs. retirement), so a small gap is "
                "fine -- but a gap this large means a GUESSED retirement target is "
                "silently outranking a MEASURED living-cost figure, and the "
                "decumulation shortfall it produces is an artifact of the guess, not "
                "of the household's finances. The decumulation sizes to the retirement "
                "target. Reconcile them, or confirm the gap is intended.",
                c["ratio"], c["living_costs"], lc_conf, c["spending_target"], st_conf,
            )

    return legacy


def load_contract_json(path: str) -> Dict:
    with open(path) as f:
        return json.load(f)


def _default_example() -> Dict:
    return load_contract_json(str(EXAMPLE_PATH))


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


