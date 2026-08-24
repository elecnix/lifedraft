"""The ``estate`` namespace: what happens on the terminal return.

Five things the engine used to SILENTLY ASSUME -- every one of them resolving
in the favourable direction -- became real declared inputs here (epic #603
Track C Phase 2c, issue #600): who dies first (from ``assumptions.mortality``,
never guessed), the spousal-rollover election folded with its per-account
overrides and weighted by balance, the TFSA successor-holder vs. beneficiary
distinction, the REAL non-registered ownership split, and life insurance
(including a term policy that lapses before the horizon).

It also owns the principal-residence-exemption arithmetic that spans the
FAMILY rather than one property (ITA s.40(2)(b), issue #969):
``_family_pre_window`` is the one spelling of the family's designation window,
read by the estate's deemed disposition AND by every voluntary sale, and
``_check_pre_family_year_conflict`` rejects a document that designates one
family-year to two properties -- loudly, at the contract boundary, before any
gain is priced.

The Canada tax arithmetic itself lives in ``countries.canada.pre_designation``
and is imported lazily (DP#25: this jurisdiction-agnostic mapper does not
depend on the Canada package at import time).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError
from contract_people import _horizon_end_year, _owner_shares, _people_by_id
from contract_property import _find_property, _property_sold_by_terminal

logger = logging.getLogger(__name__)


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
