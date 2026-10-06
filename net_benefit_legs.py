#!/usr/bin/env python3
"""The jurisdiction-dependent legs of :func:`objective.compute_net_benefit`.

Issue #232: the net-benefit arithmetic lives in ``objective.py`` (the
``max_net_benefit`` objective's fn), but some of its LEGS price jurisdiction
programs whose production consumers are exactly that formula: the Quebec LSIF
tax credits (``countries.canada.lsif_credit``), the federal iZEV / provincial
Roulez-vert incentives (``countries.canada.zev_incentive``,
``countries.canada.provinces.quebec.roulez_vert``), and the year-versioned OAS
fallback (``countries.canada.retirement.get_oas_annual_max``).

Issue #290: the terminal RRSP leg that used to live here (a retirement
re-projection on hidden constants -- a $60k expense key no contract could set,
ages from a hardcoded calendar year, a fixed retirement-age rule, one person's
brackets for the couple's RRSPs -- whose sum read a key the rows never
carried, so it priced the terminal RRSP at $0) is DELETED (DP#9, no shim). compute_net_benefit now
prices the registered balances through the estate provider seam, the same
``compute_estate`` call ``max_after_tax_estate`` makes.

``objective.py`` is the optimization layer and must stay countries-free
(DP#25/#732, enforced by ``tests/test_jurisdiction_agnostic.py``), so these
legs cannot import the jurisdiction modules themselves. Routing them through
the provider REGISTRY would not help: the static reach-detector
(``tests/architecture/test_unreached_rule_modules.py``) follows CALLS, and a
registry lookup is invisible to it -- lsif_credit / zev_incentive /
roulez_vert have no other production caller, so they would read as DEAD and
the guard would fire. The #732 precedent (estate) solved that with a real call
in a reached module; here the reached module IS this file: objective.py imports
these pure helpers, the helpers call the jurisdiction programs DIRECTLY, and
the call chain entry -> objective -> this module -> countries.canada.* is
fully visible to the detector.

DP#3 (#232): each leg is a pure function -- same inputs -> same float, no
hidden state. DP#1/DP#13 (#290): the calendar year every leg needs is the
household's own ``cfg['tax']['start_year']`` (``_tax_start_year``); an absent
year refuses loudly, it is never assumed.
"""

from countries.canada.lsif_credit import (
    LSIFPurchase, compute_lsif_credit, lsif_from_config,
)
from countries.canada.provinces.quebec.home_support import (
    home_support_credit, renter_credit,
)
from countries.canada.provinces.quebec.roulez_vert import compute_roulez_vert_rebate
from countries.canada.retirement import get_oas_annual_max
from countries.canada.zev_incentive import compute_izev_incentive, zev_purchase_from_dict
from member_config import find_member_by_role  # data layer (DP#25 #998)

def _tax_start_year(cfg) -> int:
    """The household's simulation start year, ``cfg['tax']['start_year']``
    (issue #290, DP#1/DP#13).

    ``objective.objective_cfg`` always writes it; a hand-built cfg without it
    is refused rather than silently read as a hardcoded current year (the
    pre-#290 fallback). Explicit membership tests, never
    ``.get(...) or`` (DP#32)."""
    if 'tax' not in cfg or 'start_year' not in cfg['tax']:
        raise ValueError(
            "net_benefit needs the household's calendar year, "
            "cfg['tax']['start_year'], and the cfg does not declare it. Build "
            "the objective cfg with objective.objective_cfg(config); a current "
            "year will not be assumed (DP#13/DP#32, issue #290).")
    return cfg['tax']['start_year']


# DP#13/DP#20: fallback OAS annual amount used by compute_net_benefit() when
# the household's config supplies no ``assumptions.oas_annual``. This is a
# named fallback for ABSENT input only -- the call sites reach it via a
# membership test (``'oas_annual' in assumptions``), NOT ``x or DEFAULT`` and
# NOT an eager ``dict.get`` default, so DP#32 is respected: a configured zero
# stays zero, and the fallback never runs against a supplied value (#248).
#
# Issue #1029 (the deliberate decision #986 deferred): the fallback AMOUNT is
# read live from the year-versioned government table
# ``countries.canada.retirement.get_oas_annual_max(year)`` -- the same source
# every other consumer uses (pension_split_optimizer via #331,
# simulation_rules, retirement) -- instead of a frozen literal. The relevant
# year is the household's simulation start year (``cfg['tax']['start_year']``,
# which objective.objective_cfg always writes). Issue #290: a cfg without that
# year now REFUSES (``_tax_start_year``) instead of falling back to a
# hardcoded current year.
def _default_oas_annual(cfg) -> float:
    """Year-versioned OAS maximum for ABSENT ``assumptions.oas_annual`` (#1029).

    Reads the live government table for the household's simulation start year
    (``_tax_start_year`` -- refuses when the cfg does not declare it, #290).
    Out-of-table years resolve against the nearest registered data year and
    ultimately the most recent published amount (``get_oas_annual_max``'s
    documented DP#13/DP#20 fallback) -- an absent table year becomes a
    published value, never a silent zero (DP#32).
    """
    return get_oas_annual_max(_tax_start_year(cfg))


def lsif_credit_total(cfg) -> float:
    """Total LSIF tax credit (federal + Quebec) for both members.

    Issue #231: a spouse below the LSIF income threshold is eligible for
    credits on FTQ purchases up to $5k/yr; a primary above the threshold is
    ineligible for the provincial credit. Eligibility is decided by the
    lsif_credit module from the income data in the config -- names and incomes
    are never hardcoded here.

    DP#13: birth_year is sourced from config; the placeholder (LSIFPurchase's
    default of 2000) is a clearly-dated stand-in, not a real person's year.
    """
    # DP#16: no declared LSIF block -> no purchase to price, and no calendar
    # year is needed (the same absence rule lsif_from_config applies). A
    # declared block needs the household's own year (#290 -- never 2026).
    if 'lsif' not in cfg or not cfg['lsif']:
        return 0.0
    year = _tax_start_year(cfg)
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    total = 0.0
    lsif_purchase = lsif_from_config(
        cfg, birth_year=primary.get('birth_year', LSIFPurchase.birth_year), year=year)
    if lsif_purchase is not None and lsif_purchase.amount > 0:
        lsif_result = compute_lsif_credit(lsif_purchase, year=year)
        total = lsif_result.federal_credit + lsif_result.quebec_credit

    # Also check spouse LSIF eligibility (the below-threshold spouse is the
    # typically eligible one)
    spouse_mem = find_member_by_role(members, 'spouse', {})  # #699 seam
    spouse_lsif_purchase = lsif_from_config(
        cfg, birth_year=spouse_mem.get('birth_year', LSIFPurchase.birth_year), year=year)
    if spouse_lsif_purchase is not None and spouse_lsif_purchase.amount > 0:
        spouse_lsif_result = compute_lsif_credit(spouse_lsif_purchase, year=year)
        total += spouse_lsif_result.federal_credit + spouse_lsif_result.quebec_credit
    return total


def home_support_credit_total(cfg) -> float:
    """Issue #366: the Quebec refundable home-support credit for seniors.

    Fires only when the household declares ``household_budget.home_support``
    (what it paid for eligible home-support services, or its rent, plus the
    family income and the published situation row). Absent the block this is
    0.0 and every existing household's number is byte-identical
    (DP#16/DP#32).

    The block is priced for the household's OWN year -- the same
    ``cfg['tax']['start_year']`` every leg here uses (``_tax_start_year``) --
    because the credit's rate and its two reduction thresholds are
    year-versioned data. An absent year refuses loudly rather than assuming one.

    The credit is REFUNDABLE, so unlike the tuition credit there is no tax
    payable to bound it against: what the module returns is the payment itself.

    KNOWN SIMPLIFICATION, shared verbatim with ``lsif_credit_total`` and
    ``zev_incentive_total`` above: the credit is added to the terminal objective
    undiscounted, as though received at the horizon rather than in the year it is
    paid, so it is not compounded over the years between. Correcting that means
    routing a refundable credit through the yearly fold as a real cash inflow,
    which no seam in the fold does today (no rule books a refundable credit into
    a year's cash) -- a deliberate follow-up, not a silent approximation.
    """
    if 'household_budget' not in cfg:
        return 0.0
    declared = cfg['household_budget']
    if 'home_support' not in declared:
        return 0.0
    block = declared['home_support']
    if block is None:
        return 0.0
    year = _tax_start_year(cfg)
    situation = block['situation']
    income = block['family_income']
    if block.get('monthly_rent') is not None:
        return renter_credit(year, block['monthly_rent'], income,
                             situation=situation).credit
    return home_support_credit(year, block['eligible_expenses'], income,
                               situation=situation).credit


def zev_incentive_total(cfg) -> float:
    """Total zero-emission-vehicle incentive (federal iZEV + Quebec Roulez vert).

    Fires only when the household declares a zev_purchases[] acquisition;
    absent the block this is 0.0 and every existing household's number is
    byte-identical (DP#32).

    Two INDEPENDENT programs are priced per acquisition and summed: the
    federal iZEV incentive (closed 2025-03-31) and, for a Quebec household,
    the provincial Roulez vert rebate. Each decides its own dated eligibility
    from the acquisition date -- neither reads the other, and a household may
    receive both, one, or neither.

    KNOWN SIMPLIFICATION, shared verbatim with lsif_credit_total above: the
    incentive is added to the terminal objective undiscounted, as though
    received at the horizon rather than in the acquisition year. It is
    therefore not compounded over the years between. This understates an
    early acquisition relative to a late one. Correcting it means routing the
    incentive through the yearly fold as a real inflow, which is the decision
    dimension's job, not this module's.
    """
    total = 0.0
    _province = cfg.get('tax', {}).get('province')
    for _entry in cfg.get('zev_purchases', []):
        _purchase = zev_purchase_from_dict(_entry)
        total += compute_izev_incentive(_purchase).amount
        if _province == 'quebec':
            total += compute_roulez_vert_rebate(
                acquisition_date=_purchase.acquisition_date,
                msrp=_purchase.trim_msrp,
                propulsion=_purchase.propulsion,
                is_quebec_resident=True,
            ).amount
    return total

def mg_reno_credit_total(cfg) -> float:
    """Total Multigenerational Home Renovation Tax Credit (ITA s.122.92) (#369).

    Fires only when a principal residence declares an ``mg_reno`` block;
    absent the block this is 0.0 and every existing household's number is
    byte-identical (DP#32, DP#16).

    Each qualifying renovation is priced ONCE, by the year its
    ``renovation_date`` falls in. The amount is the lowest federal bracket rate
    for that year times the least of $50,000 and the qualifying expenditures.
    Eligibility is decided inside the credit module from the household's own
    people block -- no names, ages or years are hardcoded here.

    The credit is a deemed payment ON ACCOUNT OF TAX, so it is REFUNDABLE: it
    reaches the household regardless of tax payable, and the Quebec abatement
    does not reduce it.

    KNOWN SIMPLIFICATION, shared verbatim with lsif_credit_total and
    zev_incentive_total above: the credit is added to the terminal objective
    undiscounted, as though received at the horizon rather than in the
    renovation year. It is therefore not compounded over the intervening years.
    Correcting it means routing it through the yearly fold as a real inflow,
    which is the decision dimension's job, not this module's.
    """
    from countries.canada.mg_reno_credit import (
        mg_reno_credit_for_year,
        mg_reno_from_property,
        qualifiers_from_people,
    )

    # DP#16: find the declared renovations FIRST. Building qualifiers before
    # knowing whether any renovation exists made an unrelated member's missing
    # person_id break runs that never mentioned a renovation at all -- a module
    # that has nothing to contribute must not be able to stop a run (DP#32).
    properties = cfg.get('properties')
    if properties is None:
        properties = []
    renovations = []
    for prop in properties:
        if isinstance(prop, dict) and prop.get('mg_reno') is not None:
            renovations.append((prop['mg_reno'],))
    if not renovations:
        return 0.0

    members = cfg.get('people')
    if members is None:
        family = cfg.get('family')
        members = family.get('members') if isinstance(family, dict) else None
    if members is None:
        # A renovation was declared but no people block names anyone. That is a
        # document gap, not a household with no qualifying person -- refusing
        # says so, where an empty list would credit $0 silently (DP#32).
        raise ValueError(
            "a property declares an mg_reno renovation naming qualifying "
            "people, but the document carries no people block to match those "
            "person_ids against. Refusing rather than crediting $0, which "
            "would be indistinguishable from a household that does not "
            "qualify (DP#32)."
        )
    qualifiers = qualifiers_from_people(members)

    total = 0.0
    for (facts,) in renovations:
        # facts is never None here -- the collection above filtered on
        # `prop.get('mg_reno') is not None` -- so the builder always returns a
        # renovation. The dead None-check that used to sit here was an
        # unreachable branch, not a defence.
        renovation = mg_reno_from_property(facts, qualifiers)
        total += mg_reno_credit_for_year(renovation, qualifiers)
    return total
