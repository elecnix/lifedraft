#!/usr/bin/env python3
"""The jurisdiction-dependent legs of :func:`objective.compute_net_benefit`.

Issue #232: the net-benefit arithmetic lives in ``objective.py`` (the
``max_net_benefit`` objective's fn), but three of its LEGS price jurisdiction
programs whose production consumers are exactly that formula: the RRSP
withdrawal tax (``countries.canada.retirement.project_retirement``), the
Quebec LSIF tax credits (``countries.canada.lsif_credit``), and the federal
iZEV / provincial Roulez-vert incentives (``countries.canada.zev_incentive``,
``countries.canada.provinces.quebec.roulez_vert``).

``objective.py`` is the optimization layer and must stay countries-free
(DP#25/#732, enforced by ``tests/test_jurisdiction_agnostic.py``), so these
legs cannot import the jurisdiction modules themselves. Routing them through
the provider REGISTRY would not help: the static reach-detector
(``tests/architecture/test_unreached_rule_modules.py``) follows CALLS, and a
registry lookup is invisible to it -- lsif_credit / zev_incentive /
roulez_vert / locked_in_account have no other production caller (the last via
``project_retirement``'s ``LIFFund``), so they would read as DEAD and the
guard would fire. The #732 precedent (estate) solved that with a real call in
a reached module; here the reached module IS this file: objective.py imports
these pure helpers, the helpers call the jurisdiction programs DIRECTLY, and
the call chain entry -> objective -> this module -> countries.canada.* is
fully visible to the detector.

DP#3 (#232): each leg is a pure function -- same inputs -> same float, no
hidden state. Each is a VERBATIM extraction of the block that used to sit
inline in compute_net_benefit (optimize.py before #232), so no number moves.
"""

from config_access import resolve_return_rate
from tax_calculator import tax_on_income
from tax_data import default_tax_provider
from countries.canada.lsif_credit import (
    LSIFPurchase, compute_lsif_credit, lsif_from_config,
)
from countries.canada.provinces.quebec.roulez_vert import compute_roulez_vert_rebate
from countries.canada.retirement import (
    RetirementState, get_oas_annual_max, project_retirement,
)
from countries.canada.zev_incentive import compute_izev_incentive, zev_purchase_from_dict
from member_config import find_member_by_role  # data layer (DP#25 #998)

# DP#13/DP#20: fallback OAS annual amount used by compute_net_benefit() when
# the household's config supplies no ``assumptions.oas_annual``. This is a
# named fallback for ABSENT input only -- an explicit ``0`` is honoured (the
# ``dict.get`` calls below use it as the dict.get default, NOT
# ``x or DEFAULT``, so DP#32 is respected: a configured zero stays zero).
#
# Issue #1029 (the deliberate decision #986 deferred): the fallback AMOUNT is
# read live from the year-versioned government table
# ``countries.canada.retirement.get_oas_annual_max(year)`` -- the same source
# every other consumer uses (pension_split_optimizer via #331,
# simulation_rules, retirement) -- instead of a frozen literal. The relevant
# year is the household's simulation start year (``cfg['tax']['start_year']``,
# which run_optimization always writes into the objective cfg); a hand-built
# config without that block falls back to ``_CURRENT_YEAR``, the same
# current-year convention compute_net_benefit already uses for its age and
# LSIF math. For 2026 this reads 8908 (pre-#1029 it was the stale frozen
# 8500), so optimizer net-benefit numbers MOVE for households omitting
# ``assumptions.oas_annual`` -- that delta is the intended correctness fix.
_CURRENT_YEAR = 2026


def _default_oas_annual(cfg) -> float:
    """Year-versioned OAS maximum for ABSENT ``assumptions.oas_annual`` (#1029).

    Reads the live government table for the household's simulation start year;
    an unknown year raises ValueError from ``get_oas_annual_max`` rather than
    silently coercing (DP#32).
    """
    start_year = cfg.get('tax', {}).get('start_year')
    if start_year is None:
        start_year = _CURRENT_YEAR
    return get_oas_annual_max(start_year)


def rrsp_withdrawal_tax(final, cfg) -> float:
    """RRSP -> withdrawal tax for the net-benefit objective.

    Auto-includes retirement drawdown analysis when birth_year data is
    available in cfg, otherwise uses a simplified marginal-rate estimate. The
    block moved VERBATIM from compute_net_benefit (#232) -- the price of the
    jurisdiction machinery this leg needs (project_retirement) staying out of
    objective.py.
    """
    total = 0.0
    if final.total_rrsp > 0:
        members = cfg.get('family', {}).get('members', [])
        primary = find_member_by_role(members, 'primary', {})  # #699 seam
        birth_year = primary.get('birth_year')
        if birth_year and birth_year > 1900:
            # Auto-include retirement drawdown analysis (DP#16)
            current_age = 2026 - birth_year
            # Standard retirement age: 65, or current+10 if already past 55
            retirement_age = max(current_age + 10, 65)
            # DP#19: use actual ACB tracked by simulation, not a rough estimate
            non_reg_acb = getattr(final, 'non_reg_acb',
                                   final.non_reg_balance * 0.5)  # Fallback for old results
            # DP#16/issue #232: Read CPP/OAS from config instead of hardcoding.
            # Per issue #232: retirement_income=0 was a placeholder. Now compute actual
            # retirement income from CPP monthly estimate, OAS, pension, and LIF withdrawal.
            cpp_monthly_estimated = primary.get('cpp_monthly_estimated', 0)
            cpp_start_age = primary.get('cpp_start_age', 65)
            oas_start_age = primary.get('oas_start_age', 65)
            oas_defer_months = primary.get('oas_defer_months', 0)
            pension_income_annual = primary.get('pension_income_annual', 0)
            # Compute CPP annual from monthly estimate
            cpp_annual = cpp_monthly_estimated * 12 if cpp_monthly_estimated > 0 else 0
            # Compute OAS annual from config or defaults
            _assumptions = cfg.get('assumptions', {})
            # Lazily: dict.get's default is eager, so passing
            # _default_oas_annual(cfg) would compute the fallback even when a
            # value WAS supplied -- DP#13's "never a way to coerce a value that
            # was supplied".
            oas_annual = (_assumptions['oas_annual'] if 'oas_annual' in _assumptions
                          else _default_oas_annual(cfg))
            # LIF withdrawal from simulation results (issue #230)
            lif_withdrawal = getattr(final, 'lif_withdrawal', 0)
            ret_state = RetirementState(
                rrif_balance=final.total_rrsp,  # RRSP becomes RRIF at retirement
                tfsa_balance=final.total_tfsa,
                non_reg_balance=final.non_reg_balance,
                non_reg_acb=non_reg_acb,
                age=retirement_age,
                annual_expenses=cfg.get('assumptions', {}).get('retirement_expenses', 60000),
                cpp_start_age=cpp_start_age,
                cpp_annual=cpp_annual,
                oas_annual=oas_annual,
                lif_balance=getattr(final, 'lif_balance', 0),
                lif_jurisdiction=primary.get('lira', {}).get('jurisdiction', 'federal'),
                lif_birth_year=birth_year,
            )
            ret_results = project_retirement(ret_state, investment_return=resolve_return_rate(cfg))
            total = sum(r.get('tax_owed', 0) for r in ret_results)
        else:
            # DP#13/issue #232: retirement_income should come from config.
            # Compute actual retirement income from CPP + OAS + pension + LIF.
            brackets = default_tax_provider().get_combined_brackets()
            cpp_monthly_estimated = primary.get('cpp_monthly_estimated', 0)
            cpp_annual_income = cpp_monthly_estimated * 12 if cpp_monthly_estimated > 0 else 0
            _assumptions = cfg.get('assumptions', {})
            # Lazily: dict.get's default is eager, so passing
            # _default_oas_annual(cfg) would compute the fallback even when a
            # value WAS supplied -- DP#13's "never a way to coerce a value that
            # was supplied".
            oas_annual = (_assumptions['oas_annual'] if 'oas_annual' in _assumptions
                          else _default_oas_annual(cfg))
            pension_income_annual = primary.get('pension_income_annual', 0)
            lif_withdrawal = getattr(final, 'lif_withdrawal', 0)
            retirement_income = cpp_annual_income + oas_annual + pension_income_annual + lif_withdrawal
            total = (tax_on_income(retirement_income + final.total_rrsp, brackets)
                     - tax_on_income(retirement_income, brackets))
    return total


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
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    total = 0.0
    lsif_purchase = lsif_from_config(
        cfg, birth_year=primary.get('birth_year', LSIFPurchase.birth_year), year=2026)
    if lsif_purchase is not None and lsif_purchase.amount > 0:
        lsif_result = compute_lsif_credit(lsif_purchase, year=2026)
        total = lsif_result.federal_credit + lsif_result.quebec_credit

    # Also check spouse LSIF eligibility (the below-threshold spouse is the
    # typically eligible one)
    spouse_mem = find_member_by_role(members, 'spouse', {})  # #699 seam
    spouse_lsif_purchase = lsif_from_config(
        cfg, birth_year=spouse_mem.get('birth_year', LSIFPurchase.birth_year), year=2026)
    if spouse_lsif_purchase is not None and spouse_lsif_purchase.amount > 0:
        spouse_lsif_result = compute_lsif_credit(spouse_lsif_purchase, year=2026)
        total += spouse_lsif_result.federal_credit + spouse_lsif_result.quebec_credit
    return total


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