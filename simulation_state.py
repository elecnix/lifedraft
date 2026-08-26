#!/usr/bin/env python3
"""
Simulation State — Explicit, copyable state for the simulation engine.

DP#26: The simulation step is a pure function over explicit state; `run` is a fold
over steps. SimState is the data object passed between steps, not hidden in
mutable self attributes.

DP#8/DP#9/DP#25: Canada-specific fields live in jurisdiction_state['canada'],
not as individual fields on SimState. Core simulation treats jurisdiction_state
as opaque data — it passes it through without interpreting it. Only jurisdiction
modules (countries/canada/*) read or write their own section.

Issue #25 (no backward compat): RRSP, TFSA, FHSA, RESP, SM, HELOC tracing,
spousal contribution tracking, RRSP ledger, and all other Canada-specific
fields have been removed from the SimState dataclass. They live exclusively
in jurisdiction_state['canada'] as a plain dict.

This module provides:
- SimState: Dataclass holding universal simulation state + jurisdiction_state dict
- simulate_year_pure: Pure function (state, year, action, config, return_model) → (YearResult, SimState)

References:
    DESIGN_PRINCIPLES.md — Jurisdiction State (DP#8, DP#9, DP#25)
    countries/canada/docs/GOVERNMENT_REFERENCES.md — master reference for all programs
    ITA s.146 — RRSP deduction rules
    CRA T4040 — RRSP guide
    Quebec QC deduction (TP-1 Schedule L)
"""

import dataclasses
import logging
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple
from copy import deepcopy

from simulation_config import SimulationConfig
from year_result import YearResult
# Issue #688: reserve sizing is pure, jurisdiction-agnostic arithmetic and
# lives in the same module as the waterfall that draws it (DP#25: no
# jurisdiction import; liquidation_waterfall imports nothing of ours).
from liquidation_waterfall import reserve_target

logger = logging.getLogger(__name__)

# DP#25: No direct imports from countries.canada. Jurisdiction-specific
# data flows through jurisdiction_state dict (DP#8). The adapter pattern
# (jurisdiction.py + countries/canada/adapter.py) provides concrete
# implementations that the caller injects into FamilySimulation.
#
# DP#8/DP#10 (#241): FHSA dollar limits are Canadian tax rules, not core
# constants. They are owned by countries.canada.fhsa (the FHSA rule module)
# and surface here only as fallbacks when jurisdiction_state does not carry a
# year-versioned value. Reading them from the owner — rather than re-declaring
# 8000/40000 literals in this jurisdiction-agnostic module — keeps the single
# source of truth in the Canada package (year-versioned via TaxDataProvider).
# Imported lazily to avoid any import-time cycle with the core engine.
def _canada_fhsa_limits() -> Tuple[float, float, float]:
    """(annual_limit, carry_forward_max, lifetime_limit) from the Canada FHSA module."""
    from countries.canada.fhsa import (
        FHSA_ANNUAL_LIMIT,
        FHSA_CARRY_FORWARD_MAX,
        FHSA_LIFETIME_LIMIT,
    )
    return FHSA_ANNUAL_LIMIT, FHSA_CARRY_FORWARD_MAX, FHSA_LIFETIME_LIMIT

# ── Locked-in-account / LIF conversion registry (DP#25/DP#10, issue #283) ────
# Per DP#25 dependencies point inward: the simulation layer (this module) must
# not import jurisdiction code such as `countries.canada.locked_in_account`.
# Instead, the country package registers a *provider* exposing the locked-in /
# LIF operations the pure step needs, and `simulate_year_pure` calls through the
# registered interface. The Canada package pushes its provider in at import time
# (DP#16 package-presence trigger), mirroring the merged #240/#284 inversion.
#
# A LIF-conversion provider must expose:
#   must_convert_by_year(birth_year)            -> calendar year of CRI/LIRA→LIF
#       mandatory backstop (end of year owner turns 71)
#   lif_conversion_year(birth_year, jurisdiction, election_year=None) -> calendar
#       year the LIRA actually converts (issue #708): the earlier of an elected
#       conversion date and the age-71 backstop; early elections are honoured
#       down to the jurisdiction's earliest-permitted age (Quebec: none) and
#       rejected (raise) for unsourced jurisdictions rather than guessed.
#   make_locked_in_account(balance, birth_year, jurisdiction) -> account
#       with .convert_to_lif(year, reference_rate) -> (lif_fund, depleted)
#   make_lif_fund(balance, owner_birth_year, reference_rate, jurisdiction) -> fund
#       with .minimum_withdrawal(year) / .maximum_withdrawal(year)
#            / .withdraw(amount, year) -> (actual, fund)
#            / .grow(rate) -> (_, fund); each fund carries .balance,
#            .owner_birth_year, .jurisdiction, .reference_rate.
# The shapes mirror countries.canada.locked_in_account exactly so behaviour is
# byte-identical to the previous direct-import path (DP#25, issue #283).
_LIF_CONVERSION_PROVIDER = None


def register_lif_conversion_provider(provider) -> None:
    """Register the jurisdiction LIF-conversion provider (DP#25, issue #283).

    Called by country packages (e.g. countries.canada) at import time so the
    simulation layer never imports jurisdiction code. ``provider`` must supply
    ``must_convert_by_year``, ``make_locked_in_account`` and ``make_lif_fund``
    (see module docstring above for the contract).
    """
    global _LIF_CONVERSION_PROVIDER
    _LIF_CONVERSION_PROVIDER = provider


def _get_lif_conversion_provider():
    """Return the registered LIF-conversion provider.

    The Canada package registers its provider on import. If a jurisdiction with
    a locked-in account is configured but no provider was registered, importing
    that package was skipped — surface a clear error rather than silently
    dropping the LIRA→LIF conversion.
    """
    if _LIF_CONVERSION_PROVIDER is None:
        raise RuntimeError(
            "No LIF-conversion provider registered. A locked-in account "
            "(CRI/LIRA) is present but the owning jurisdiction package was not "
            "imported, so its conversion provider was never registered "
            "(DP#25, issue #283). Import the country package (e.g. "
            "`import countries.canada`) before running the simulation."
        )
    return _LIF_CONVERSION_PROVIDER

# RRSP ledger is now a plain list of dicts (DP#19: track per-contribution
# deduction data) rather than a Canada-specific class. This keeps
# simulation_state.py jurisdiction-agnostic.


class RRSPListLedger:
    """Plain-list RRSP ledger wrapper (jurisdiction-agnostic, DP#25).

    The canonical per-contribution RRSP deduction ledger. Stores entries as
    plain dicts (DP#25: jurisdiction-agnostic) so the simulation fold carries
    no Canada-specific class. The dead countries.canada.rrsp_ledger clone that
    once shadowed this was removed (#744, DP#9).
    """
    def __init__(self, entries: list = None):
        self._entries = entries or []

    @property
    def contributions(self):
        """Access entries list."""
        return self._entries

    @contributions.setter
    def contributions(self, value):
        self._entries = value

    def undeducted_total(self) -> float:
        """Total undeducted contribution amount."""
        return sum(e['amount'] for e in self._entries if not e['deducted'])

    def total_deducted(self) -> float:
        """Total deducted contribution amount."""
        return sum(e['amount'] for e in self._entries if e['deducted'])

    def total_tax_savings(self) -> float:
        """Total tax savings from all deducted contributions."""
        return sum(
            e['amount'] * (e.get('deduction_marginal_rate') or 0)
            for e in self._entries if e['deducted']
        )

    def add_contribution(self, year: int, amount: float, role: str = 'primary'):
        """Add a contribution entry."""
        self._entries.append({
            'year': year, 'amount': amount, 'role': role,
            'deducted': False, 'deduction_year': None,
            'deduction_marginal_rate': None,
        })

    def clone(self) -> 'RRSPListLedger':
        """Return an independent copy (shallow dict copies of entries).

        Issue #1059: entries are flat dicts of scalars, so a shallow copy
        per entry ({**e}) is sufficient.  The returned ledger shares no
        mutable containers with self (DP#26).
        """
        return RRSPListLedger([{**e} for e in self._entries])

    def claim_all_deductions(self, year: int, marginal_rate: float):
        """Claim all undeducted contributions."""
        for e in self._entries:
            if not e['deducted']:
                e['deducted'] = True
                e['deduction_year'] = year
                e['deduction_marginal_rate'] = marginal_rate

    def claim_deferred_deduction(self, year: int, income: float, brackets: list,
                                 bracket_target: float = 0.0) -> dict:
        """Claim the deduct-later slice for ``year`` at bracket-fill rates (DP#19).

        Deducts undeducted primary/spousal contributions down to
        ``bracket_target`` (the income floor the contributor wants to keep taxed
        at the higher bracket). Each claimed slice is valued at the marginal
        rate of the income band it removes, not a flat top rate (issue #546):
        as income is drawn toward ``bracket_target`` the slices fall through
        progressively lower brackets.

        Mutates the ledger (marks entries deducted, splitting partial claims).
        Returns a dict with ``savings``, ``amount`` deducted, and ``claims`` —
        a per-entry list of (year, amount, rate) for output surfacing.
        """
        from tax_calculator import deduction_value, marginal_rate

        undeducted = self.undeducted_total()
        if undeducted <= 0:
            return {'savings': 0.0, 'amount': 0.0, 'claims': []}

        if bracket_target <= 0 and income > 0:
            mtr = marginal_rate(income, brackets)
            for i in range(len(brackets) - 1, 0, -1):
                if brackets[i]['min'] < income and brackets[i]['rate'] >= mtr - 0.10:
                    bracket_target = brackets[i]['min']
                    break
            if bracket_target <= 0:
                bracket_target = brackets[3]['min'] if len(brackets) > 3 else 50000

        amount_to_deduct = min(undeducted, max(0.0, income - bracket_target))
        if amount_to_deduct <= 0:
            return {'savings': 0.0, 'amount': 0.0, 'claims': []}

        running_income = income
        remaining = amount_to_deduct
        total_savings = 0.0
        claims = []
        for entry in list(self._entries):
            if remaining <= 0:
                break
            if entry['deducted'] or entry['role'] not in ('primary', 'spousal'):
                continue
            claim_from = min(remaining, entry['amount'])
            slice_savings = deduction_value(running_income, claim_from, brackets)
            slice_rate = slice_savings / claim_from if claim_from else 0.0
            total_savings += slice_savings
            running_income -= claim_from
            remaining -= claim_from
            claims.append({'year': entry['year'], 'amount': claim_from,
                           'rate': slice_rate})
            if claim_from < entry['amount']:
                entry['amount'] -= claim_from
                self.add_contribution(year=entry['year'], amount=claim_from,
                                      role=entry['role'])
                self._entries[-1]['deducted'] = True
                self._entries[-1]['deduction_year'] = year
                self._entries[-1]['deduction_marginal_rate'] = slice_rate
            else:
                entry['deducted'] = True
                entry['deduction_year'] = year
                entry['deduction_marginal_rate'] = slice_rate

        return {'savings': total_savings, 'amount': amount_to_deduct,
                'claims': claims}

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)


# ── Default values for Canada-specific jurisdiction_state keys ──────────────
# These defaults are used by __post_init__ and initial() when constructing
# the jurisdiction_state['canada'] dict. They are NOT imports from
# countries.canada (DP#25).

def _default_canada_state() -> dict:
    """Return a fresh jurisdiction_state['canada'] dict with default values.

    All Canada-specific simulation fields live here (DP#9, DP#25, issue #25).
    Core simulation treats this dict as opaque data (DP#8).
    """
    return {
        # RRSP accounts (issue #700/#643): per-adult store, one entry per adult
        # id, in canonical adult order (primary first). Each entry is
        # {'own', 'own_room', 'spousal_as_annuitant'} -- the adult's own RRSP,
        # their own contribution room, and any spousal RRSP they are the
        # annuitant of (ITA s.146(8.3), contributed by their partner). Replaces
        # the three hardcoded pots rrsp/spouse_rrsp/spousal_rrsp: it holds the
        # same money for two adults but is not capped at two. Seeded empty;
        # SimState.initial populates it.
        'adult_rrsp': {},

        # TFSA accounts (issue #700/#643): per-adult store, one entry per adult
        # id in canonical adult order (primary first). Each entry is
        # {'balance', 'room'}. Replaces the two hardcoded pots
        # tfsa_primary/tfsa_spouse: same money for two adults, not capped at two.
        # Seeded empty; SimState.initial populates it.
        'adult_tfsa': {},

        # RESP (per-child balances and composition buckets, issue #578).
        # contributions/cesg/qesi are cost-basis-like (DP#19): they only
        # change via new contributions or withdrawals, never via growth.
        'resp_balances': [],
        'resp_contributions': [],
        'resp_cesg': [],
        'resp_qesi': [],

        # Smith Manoeuvre
        'readvance_heloc_balance': 0.0,
        'sm_investment_balance': 0.0,
        'sm_investment_cost_basis': 0.0,
        'readvance_total_interest_paid': 0.0,
        'readvance_total_tax_saved': 0.0,

        # HELOC tracing (DP#6, ITA §20(1)(c))
        'heloc_tracing': _default_heloc_tracing(),

        # Issue #850: purpose tracing for the OTHER two borrowings a year-0
        # leveraged lump sum creates -- the mortgage ADVANCE (cash-out) and the
        # DRAWN revolving margin. Distinct from 'heloc_tracing' above, which
        # traces the SM READVANCE line ('readvance_heloc_balance'), a third and
        # separate balance. Booked once at year 0 by the 'borrowing_purpose'
        # rule from borrowing_purpose_tracings() and then carried FORWARD
        # unchanged: the purpose of a borrowing is fixed when the money is
        # spent, not re-decided every year. All-zero (hence a 0.0 deductible
        # proportion, hence fully inert) for a household that took no lump sum
        # -- e.g. the golden household (DP#32).
        'advance_tracing': _default_heloc_tracing(),
        'margin_tracing': _default_heloc_tracing(),

        # Quebec deduction carry-forward
        'qc_carry_forward': 0.0,

        # Issue #747: minimum-tax credit carry-forward balances (ITA s.120.2;
        # Revenu Québec TP-776.42). Each a list of AMTCredit (frozen year+amount)
        # -- AMT / Quebec-IMR paid in excess of regular tax, recoverable against
        # regular tax in a later year and expiring after 7 years. The `amt` rule
        # reads the opening balance off ctx and writes the closing balance back
        # here (both empty for a household that never pays a minimum tax -- the
        # golden household -- so this is inert, DP#32).
        'amt_credit_buckets': [],
        'qc_imr_credit_buckets': [],

        # Issue #784: per-member unused tuition-tax-credit carry-forward. The
        # federal + Quebec tuition credits (#764/#783) are NON-REFUNDABLE;
        # the unused portion (credit > tax) carries forward to a future year
        # (CRA / Revenu Québec indefinite carry-forward). The prologue
        # (simulation.py, both time-steps) reads the opening carry-forward,
        # applies the capped credit, and writes the new remainder here. 0.0
        # for a household that declares no tuition (inert, DP#32). Lives in
        # jurisdiction_state['canada'] because it is a Canada-specific tax
        # construct (DP#25: no Canada fields at SimState top level).
        'primary_tuition_carryforward': 0.0,
        'spouse_tuition_carryforward': 0.0,
        # Issue #785: per-child unused tuition-credit carry-forward (the
        # remainder after transfer to a supporting parent/spouse). A list
        # parallel to SimulationConfig.children, initialized to all 0.0.
        'child_tuition_carryforwards': [],

        # Epic #841 bite 2 / issue #812: each child's OWN registered accounts
        # (TFSA/FHSA/RRSP/non-reg) -- balances + available room -- as a list
        # parallel to SimulationConfig.children. A child is a first-class
        # savings subject (#841): their OWN income funds contributions into
        # their OWN accounts, which grow year over year in the fold, entirely
        # separate from the household (primary/spouse) pot. NOT counted in
        # total_assets() -- the family objective that sums across all members
        # is a later bite (#841 bite 4); here the accounts are MODELLED and
        # threaded, not yet aggregated. Empty for a household that declares no
        # children, so this is inert for the golden household (DP#32).
        'child_accounts': [],

        # Spousal RRSP attribution tracking (ITA s.146(8.3))
        'spousal_contribution_years': [],

        # RRSP per-contribution deduction ledger (DP#19)
        'rrsp_ledger': [],

        # Deduct-later carry-forward (DP#45)
        'rrsp_deduction_carry_forward': 0.0,

        # Issue #546: deduct-later advantage vs deducting the whole lump in one
        # year. Tracked as the running staggered bracket-fill total, the income
        # of the first year a deferred slice was claimed, and the total amount
        # deducted so far; the surfaced scalar is staggered_total minus the
        # bracket-fill value of deducting that total all in the first year.
        'deduct_later_staggered_total': 0.0,
        'deduct_later_first_claim_income': 0.0,
        'deduct_later_total_deducted': 0.0,

        # Cumulative RRSP refund applied to HELOC paydown
        'heloc_rrsp_paydown': 0.0,

        # FHSA / CRI-LIRA / LIF (issue #700/#643/#704): per-adult stores, one
        # entry per adult id in canonical order (primary first), NOT one
        # singleton household pot each. Each FHSA entry is
        # {'balance','room','lifetime_used','lifetime_limit'}; each LIRA entry is
        # {'balance','birth_year','jurisdiction','reference_rate','conversion_year'};
        # each LIF entry is {'balance','birth_year','jurisdiction','reference_rate'}.
        # (CRI/LIRA: locked-in retirement account, no withdrawals except
        # hardship/unlock. LIF: Life Income Fund, created from CRI/LIRA
        # conversion at age 71. DP#8/DP#25: jurisdiction data flows through the
        # jurisdiction_state dict.) Seeded empty; SimState.initial populates them.
        'adult_fhsa': {},
        'adult_lira': {},
        'adult_lif': {},

        # Issue #931: per-adult open Home Buyers' Plan records, keyed by the
        # buyer's member id -> {'slot': 0/1, 'withdrawal', 'withdrawal_year',
        # 'repaid', 'outstanding', 'repayment_schedule'}. Populated only when an
        # ADULT declares a first_home_purchases[] entry and carried forward until
        # the 15-year repayment restores the RRSP. Empty {} for the golden
        # household (no adult first-home purchase) -> inert (DP#32).
        'adult_hbp': {},

        # Issue #694 (epic #690 bite 3): per-rental-property undepreciated
        # capital cost (UCC), keyed by property id. A rental that elects Capital
        # Cost Allowance depreciates its building each year; the fold reads the
        # opening UCC here (falling back to the declared opening_ucc the first
        # year a property is absent), claims the year's CCA, and writes the
        # closing UCC back so the estate can recapture it at the deemed
        # disposition. Empty {} for a household with no CCA election (the golden
        # path -- inert, DP#32).
        'rental_ucc': {},
    }


# ── Issue #700/#643: per-adult RRSP store accessors ─────────────────────────
# canada_state['adult_rrsp'] is an ordered dict {adult_id: {'own', 'own_room',
# 'spousal_as_annuitant'}} in canonical adult order (primary first, then
# spouse). These helpers are the single seam every reader of the store goes
# through, so the still-two-slot compute (WorkingState scalars) and the N-adult
# storage meet in exactly one place. Isomorphic to the old three flat pots for a
# two-adult household: slot 0 == the old rrsp_balance/rrsp_room; slot 1 == the
# old spouse_rrsp_balance/spouse_rrsp_room, and its spousal_as_annuitant == the
# old spousal_rrsp_balance (the annuitant is the spouse).

def adult_rrsp_slot(canada: dict, index: int):
    """The ``index``-th adult's RRSP entry (0=primary, 1=spouse) as the tuple
    ``(own, own_room, spousal_as_annuitant)``.

    A missing slot -- a household with fewer adults than ``index+1`` -- reads as
    zeros, the same absence the old flat ``spouse_rrsp_*`` keys encoded via
    ``.get(key, 0)`` (DP#32: absence is a real zero here, not a fabricated one).
    """
    entries = list(canada.get('adult_rrsp', {}).values())
    if 0 <= index < len(entries):
        e = entries[index]
        return e.get('own', 0.0), e.get('own_room', 0.0), e.get('spousal_as_annuitant', 0.0)
    return 0.0, 0.0, 0.0


def adult_rrsp_total(canada: dict) -> float:
    """Household RRSP fair-market value summed over the per-adult store: each
    adult's own RRSP plus any spousal RRSP they are the annuitant of. Byte-for-
    byte the old ``rrsp_balance + spousal_rrsp_balance + spouse_rrsp_balance``
    sum for a two-adult household."""
    return sum(e.get('own', 0.0) + e.get('spousal_as_annuitant', 0.0)
               for e in canada.get('adult_rrsp', {}).values())


def rebuild_adult_rrsp(prior_adult_rrsp: dict, *, primary_own: float,
                       primary_room: float, spouse_own: float,
                       spouse_room: float, spousal_as_annuitant: float) -> dict:
    """Return a FRESH per-adult RRSP store carrying ``prior_adult_rrsp``'s ids
    and order but the post-fold balances from the (two-slot) WorkingState.

    Writeback must not mutate the prior state's nested objects in place (see the
    writeback contract in ``build_year_result``): every entry here is a new dict.
    Slot 0 (primary) takes the primary scalars; slot 1 (spouse) takes the spouse
    scalars and the spousal-annuitant balance; any further adults (not yet
    driven by the two-slot compute -- admitted only at Step 8/#698) are carried
    forward unchanged.
    """
    new_store: dict = {}
    for i, aid in enumerate(prior_adult_rrsp):
        if i == 0:
            new_store[aid] = {'own': primary_own, 'own_room': primary_room,
                              'spousal_as_annuitant': 0.0}
        elif i == 1:
            new_store[aid] = {'own': spouse_own, 'own_room': spouse_room,
                              'spousal_as_annuitant': spousal_as_annuitant}
        else:
            new_store[aid] = dict(prior_adult_rrsp[aid])
    return new_store


# ── Issue #700/#643: per-adult TFSA store accessors ─────────────────────────
# canada_state['adult_tfsa'] is an ordered dict {adult_id: {'balance', 'room'}}
# in canonical adult order (primary first, then spouse). Same seam pattern as
# adult_rrsp: slot 0 == the old tfsa_primary_*; slot 1 == the old tfsa_spouse_*.

def adult_tfsa_slot(canada: dict, index: int):
    """The ``index``-th adult's TFSA entry (0=primary, 1=spouse) as the tuple
    ``(balance, room)``. A missing slot reads as zeros (DP#32: a real absence)."""
    entries = list(canada.get('adult_tfsa', {}).values())
    if 0 <= index < len(entries):
        e = entries[index]
        return e.get('balance', 0.0), e.get('room', 0.0)
    return 0.0, 0.0


def adult_tfsa_total(canada: dict) -> float:
    """Household TFSA fair-market value summed over the per-adult store --
    byte-for-byte the old ``tfsa_primary_balance + tfsa_spouse_balance`` sum."""
    return sum(e.get('balance', 0.0) for e in canada.get('adult_tfsa', {}).values())


def rebuild_adult_tfsa(prior_adult_tfsa: dict, *, primary_balance: float,
                       primary_room: float, spouse_balance: float,
                       spouse_room: float) -> dict:
    """Return a FRESH per-adult TFSA store carrying ``prior_adult_tfsa``'s ids
    and order but the post-fold balances from the (two-slot) WorkingState (same
    no-in-place-mutation writeback contract as ``rebuild_adult_rrsp``)."""
    new_store: dict = {}
    for i, aid in enumerate(prior_adult_tfsa):
        if i == 0:
            new_store[aid] = {'balance': primary_balance, 'room': primary_room}
        elif i == 1:
            new_store[aid] = {'balance': spouse_balance, 'room': spouse_room}
        else:
            new_store[aid] = dict(prior_adult_tfsa[aid])
    return new_store


# ── Issue #700/#643/#704: per-adult FHSA / LIRA / LIF store accessors ────────
# Step 4 of #643 replaces the three SINGLETON household pots (one fhsa_* / one
# lira_* / one lif_* each) with per-adult stores keyed by the stable entity id
# in canonical adult order (primary first):
#     adult_fhsa = {adult_id: {'balance','room','lifetime_used','lifetime_limit'}}
#     adult_lira = {adult_id: {'balance','birth_year','jurisdiction',
#                              'reference_rate','conversion_year'}}
#     adult_lif  = {adult_id: {'balance','birth_year','jurisdiction',
#                              'reference_rate'}}
# Slot 0 is the account the still-single-slot compute drives (WorkingState
# scalars), byte-for-byte the old singleton. A SECOND adult's FHSA (now
# representable once input_contract's dual-owner refusal is relaxed) lives in
# slot 1 and COMPOUNDS via rebuild_adult_fhsa's growth pass -- but receives no
# per-adult contribution/room this step, and a second LIRA/LIF's conversion
# mechanics are likewise out of scope (both DEFERRED, tracked as the Step 4
# follow-up). For a household with <=1 owner of each kind (the golden path) the
# store reduces to a single entry, so the golden invariant is byte-identical.


def adult_fhsa_slot(canada: dict, index: int) -> dict:
    """The ``index``-th adult's FHSA entry as a dict with every field defaulted
    (0=primary drives the compute). A missing slot reads as an empty FHSA with
    the module's lifetime limit (DP#32: a real absence, mirroring the old
    ``canada.get('fhsa_lifetime_limit', _canada_fhsa_limits()[2])``)."""
    entries = list(canada.get('adult_fhsa', {}).values())
    _limit = _canada_fhsa_limits()[2]
    if 0 <= index < len(entries):
        e = entries[index]
        return {
            'balance': e.get('balance', 0.0),
            'room': e.get('room', 0.0),
            'lifetime_used': e.get('lifetime_used', 0.0),
            'lifetime_limit': e.get('lifetime_limit', _limit),
        }
    return {'balance': 0.0, 'room': 0.0, 'lifetime_used': 0.0, 'lifetime_limit': _limit}


def adult_fhsa_total(canada: dict) -> float:
    """Household FHSA fair-market value summed over the per-adult store --
    byte-for-byte the old single ``fhsa_balance`` for a one-owner household."""
    return sum(e.get('balance', 0.0) for e in canada.get('adult_fhsa', {}).values())


def adult_fhsa_total_room(canada: dict) -> float:
    """Household FHSA contribution room summed over every owner (issue #893).
    The strategy sizes the year's household FHSA budget against this TOTAL so a
    SECOND owner's room is actually funded; the per-owner fill (each capped to
    its own room) happens in ``rebuild_adult_fhsa``. For a one-owner household
    this IS slot 0's room, so it is byte-identical to the old slot-0 read."""
    return sum(max(0.0, e.get('room', 0.0))
               for e in canada.get('adult_fhsa', {}).values())


def adult_fhsa_total_lifetime_remaining(canada: dict) -> float:
    """Household FHSA lifetime headroom summed over every owner (issue #893) --
    the lifetime analogue of ``adult_fhsa_total_room``. One owner => slot 0's
    lifetime remaining, byte-identical to the old slot-0 read."""
    _limit = _canada_fhsa_limits()[2]
    return sum(max(0.0, e.get('lifetime_limit', _limit) - e.get('lifetime_used', 0.0))
               for e in canada.get('adult_fhsa', {}).values())


def adult_fhsa_active(canada: dict) -> bool:
    """Whether ANY adult holds an FHSA (a balance or unused room) -- the
    per-adult form of the old ``fhsa_room > 0 or fhsa_balance > 0`` probe."""
    return any(e.get('balance', 0.0) > 0 or e.get('room', 0.0) > 0
               for e in canada.get('adult_fhsa', {}).values())


def _fhsa_step_further_owner(entry: dict, *, contribution: float,
                             annual_limit, growth: float):
    """One year's FHSA step for a FURTHER owner (slot >= 1), issue #893: fund up
    to the owner's OWN room and lifetime remaining, grow, then re-accrue this
    year's annual room -- the per-owner analogue of slot 0's ``apply_fhsa``
    (simulation_rules.py). Returns ``(new_entry, leftover_contribution)`` so a
    fixed household budget fills owners in order without ever double-spending or
    exceeding any owner's own room."""
    _limit = _canada_fhsa_limits()[2]
    room = entry.get('room', 0.0)
    lifetime_used = entry.get('lifetime_used', 0.0)
    lifetime_limit = entry.get('lifetime_limit', _limit)
    lifetime_remaining = max(0.0, lifetime_limit - lifetime_used)
    used = min(max(0.0, contribution), max(0.0, room), lifetime_remaining)
    new_balance = (entry.get('balance', 0.0) + used) * (1 + growth)
    new_room = max(0.0, room - used)
    if annual_limit is not None:
        new_room = min(new_room, _canada_fhsa_limits()[1]) + annual_limit
    new_entry = {**entry, 'balance': new_balance, 'room': new_room,
                 'lifetime_used': lifetime_used + used, 'lifetime_limit': lifetime_limit}
    return new_entry, max(0.0, contribution - used)


def rebuild_adult_fhsa(prior_adult_fhsa: dict, *, balance: float, room: float,
                       lifetime_used: float, lifetime_limit: float,
                       growth: float, overflow: float = 0.0,
                       annual_limit=None) -> dict:
    """Return a FRESH per-adult FHSA store carrying ``prior_adult_fhsa``'s ids
    and order. Slot 0 gets the post-fold compute scalars (byte-for-byte the old
    singleton). Issue #893: the household FHSA budget slot 0 could not absorb
    (``overflow`` = ``fhsa_contribution`` minus slot 0's actual contribution)
    now spills into each FURTHER owner's OWN FHSA -- capped to that owner's own
    room and lifetime, room re-accruing ``annual_limit`` -- filling owners in
    order. With no overflow a further owner's balance simply COMPOUNDS at
    ``growth`` (the Step 4 growth-only behaviour), so a single-owner household
    (``overflow`` == 0, no slot 1) is byte-identical."""
    new_store: dict = {}
    remaining_overflow = max(0.0, overflow)
    for i, aid in enumerate(prior_adult_fhsa):
        if i == 0:
            new_store[aid] = {
                'balance': balance, 'room': room,
                'lifetime_used': lifetime_used, 'lifetime_limit': lifetime_limit,
            }
        else:
            new_store[aid], remaining_overflow = _fhsa_step_further_owner(
                prior_adult_fhsa[aid], contribution=remaining_overflow,
                annual_limit=annual_limit, growth=growth)
    return new_store


def adult_lira_total(canada: dict) -> float:
    """Household LIRA balance summed over the per-adult store."""
    return sum(e.get('balance', 0.0) for e in canada.get('adult_lira', {}).values())


def adult_lif_total(canada: dict) -> float:
    """Household LIF balance summed over the per-adult store."""
    return sum(e.get('balance', 0.0) for e in canada.get('adult_lif', {}).values())


def adult_lira_slot(canada: dict, index: int) -> dict:
    """The ``index``-th adult's LIRA entry with every field defaulted (0=primary
    drives the single-slot conversion compute). Absence mirrors the old
    ``lira_*`` defaults (federal / 6% / no election)."""
    entries = list(canada.get('adult_lira', {}).values())
    if 0 <= index < len(entries):
        e = entries[index]
        return {
            'balance': e.get('balance', 0.0),
            'birth_year': e.get('birth_year', 0),
            'jurisdiction': e.get('jurisdiction', 'federal'),
            'reference_rate': e.get('reference_rate', 0.06),
            'conversion_year': e.get('conversion_year', 0),
        }
    return {'balance': 0.0, 'birth_year': 0, 'jurisdiction': 'federal',
            'reference_rate': 0.06, 'conversion_year': 0}


def adult_lif_slot(canada: dict, index: int) -> dict:
    """The ``index``-th adult's LIF entry with every field defaulted (0=primary
    drives the single-slot conversion compute)."""
    entries = list(canada.get('adult_lif', {}).values())
    if 0 <= index < len(entries):
        e = entries[index]
        return {
            'balance': e.get('balance', 0.0),
            'birth_year': e.get('birth_year', 0),
            'jurisdiction': e.get('jurisdiction', 'federal'),
            'reference_rate': e.get('reference_rate', 0.06),
        }
    return {'balance': 0.0, 'birth_year': 0, 'jurisdiction': 'federal',
            'reference_rate': 0.06}


def rebuild_adult_lira(prior_adult_lira: dict, *, balance: float, birth_year: int,
                       jurisdiction: str, reference_rate: float,
                       conversion_year: int) -> dict:
    """Return a FRESH per-adult LIRA store carrying ``prior_adult_lira``'s ids
    and order. Slot 0 gets the post-fold conversion scalars; further slots are
    carried unchanged (2-owner conversion mechanics DEFERRED, Step 4 follow-up)."""
    new_store: dict = {}
    for i, aid in enumerate(prior_adult_lira):
        if i == 0:
            new_store[aid] = {
                'balance': balance, 'birth_year': birth_year,
                'jurisdiction': jurisdiction, 'reference_rate': reference_rate,
                'conversion_year': conversion_year,
            }
        else:
            new_store[aid] = dict(prior_adult_lira[aid])
    return new_store


def rebuild_adult_lif(prior_adult_lif: dict, *, balance: float, birth_year: int,
                      jurisdiction: str, reference_rate: float) -> dict:
    """Return a FRESH per-adult LIF store carrying ``prior_adult_lif``'s ids and
    order. Slot 0 gets the post-fold conversion scalars; further slots are
    carried unchanged (2-owner conversion mechanics DEFERRED, Step 4 follow-up)."""
    new_store: dict = {}
    for i, aid in enumerate(prior_adult_lif):
        if i == 0:
            new_store[aid] = {
                'balance': balance, 'birth_year': birth_year,
                'jurisdiction': jurisdiction, 'reference_rate': reference_rate,
            }
        else:
            new_store[aid] = dict(prior_adult_lif[aid])
    return new_store


def _convert_one_locked_in(lira_entry: dict, lif_entry: dict,
                           calendar_year: int, investment_return: float):
    """One owner's CRI/LIRA growth + LIRA->LIF conversion for a projection year
    (issue #893) -- the per-owner analogue of the slot-0 ``apply_lira_lif`` rule
    (simulation_rules.py), for a single (LIRA, LIF) pair. Returns
    ``(new_lira_entry, new_lif_entry)``.

    The conversion year is birth-year driven (mandatory age-71 backstop) or the
    owner's elected year, whichever is earlier, resolved per the owner's own
    jurisdiction and reference rate. Before conversion the LIRA compounds; on
    conversion the locked-in balance is relabelled into the LIF (money
    conserved); afterward the LIF compounds. The LIF's mandatory minimum
    withdrawal and its income/tax attribution for a FURTHER owner are the
    deferred two-role decumulation work (#698-706): withdrawing here with no
    downstream income sink would leak money (trajectory money conservation), so
    a further owner's post-conversion LIF grows without a forced draw."""
    lira_bal = lira_entry.get('balance', 0.0)
    lira_by = lira_entry.get('birth_year', 0)
    lira_jur = lira_entry.get('jurisdiction', 'federal')
    lira_ref = lira_entry.get('reference_rate', 0.06)
    lira_conv = lira_entry.get('conversion_year', 0)
    lif_bal = lif_entry.get('balance', 0.0)
    lif_by = lif_entry.get('birth_year', 0)
    lif_jur = lif_entry.get('jurisdiction', 'federal')
    lif_ref = lif_entry.get('reference_rate', 0.06)

    new_lira_bal = lira_bal
    new_lif_bal = lif_bal
    new_lif_by = lif_by
    new_lif_jur = lif_jur
    new_lif_ref = lif_ref

    provider = None
    if (lira_bal > 0 and lira_by > 0) or (lif_bal > 0 and lif_by > 0):
        provider = _get_lif_conversion_provider()

    if lira_bal > 0 and lira_by > 0:
        convert_year = provider.lif_conversion_year(
            lira_by, lira_jur, lira_conv if lira_conv > 0 else None)
        if calendar_year >= convert_year:
            account = provider.make_locked_in_account(
                balance=lira_bal, birth_year=lira_by, jurisdiction=lira_jur)
            lif_fund, _ = account.convert_to_lif(
                calendar_year, reference_rate=lira_ref)
            new_lira_bal = 0.0
            new_lif_bal = lif_fund.balance
            new_lif_by = lif_fund.owner_birth_year
            new_lif_jur = lif_fund.jurisdiction
            new_lif_ref = lif_fund.reference_rate
        else:
            new_lira_bal = lira_bal * (1 + investment_return)

    if lif_bal > 0 and lif_by > 0 and new_lira_bal == 0:
        # Already-converted LIF from a prior year: compound (forced withdrawal
        # deferred -- see docstring).
        new_lif_bal = lif_bal * (1 + investment_return)
    elif lif_bal > 0 and lira_bal > 0:
        # Defensive: a LIF exists but the LIRA has not converted yet.
        new_lif_bal = lif_bal * (1 + investment_return)

    new_lira = {**lira_entry, 'balance': new_lira_bal}
    new_lif = {**lif_entry, 'balance': new_lif_bal, 'birth_year': new_lif_by,
               'jurisdiction': new_lif_jur, 'reference_rate': new_lif_ref}
    return new_lira, new_lif


def convert_further_adult_locked_in(lira_store: dict, lif_store: dict,
                                    calendar_year: int,
                                    investment_return: float):
    """Apply the per-owner CRI/LIRA growth + LIRA->LIF conversion to every
    FURTHER owner (slot >= 1) of the locked-in stores (issue #893). Slot 0 is
    left EXACTLY as passed -- it is already driven by the single-slot compute
    (``apply_lira_lif``) -- so a household with <=1 locked-in owner (the golden
    path) is untouched: a pure no-op. Returns ``(new_lira_store, new_lif_store)``
    carrying the same ids/order; the LIRA and LIF of the SAME owner are converted
    together (the balance moves from one store to the other atomically)."""
    lira_ids = list(lira_store)
    new_lira = dict(lira_store)
    new_lif = dict(lif_store)
    for i, aid in enumerate(lira_ids):
        if i == 0:
            continue
        lif_entry = lif_store.get(aid, {'balance': 0.0, 'birth_year': 0,
                                        'jurisdiction': 'federal',
                                        'reference_rate': 0.06})
        conv_lira, conv_lif = _convert_one_locked_in(
            lira_store[aid], lif_entry, calendar_year, investment_return)
        new_lira[aid] = conv_lira
        new_lif[aid] = conv_lif
    return new_lira, new_lif


# ── Epic #841 bite 2 / issue #812: a child's OWN accounts in the fold ────────
# Bite 1 (#844) made a child-owned account + child income REACH the engine as
# data; bite 2 MODELS them: a child's own income funds contributions into the
# child's own registered accounts, which then grow year over year. These three
# helpers are the whole of that -- seeding, per-year savings, and the per-year
# growth step -- kept as small pure functions (DP#26) so both time-step paths
# and the optimizer fold share ONE spelling (DP#9).

def _initial_child_accounts(children: list) -> list:
    """Seed each child's OWN opening registered accounts from the contract
    (epic #841 bite 2). Parallel to ``config.children`` by index.

    Balances come from the child-owned rrsp/tfsa/fhsa opening balances bite 1
    (#844) already routes onto the child member dict; room comes from the
    child's declared accumulated room. ``.get(key, 0.0)`` fires only on genuine
    absence (a child who owns no such account / has no such room) -- it never
    overrides a supplied 0.0 (DP#32). A child with nothing still gets an entry
    (all zeros): a MODELLED zero saver, not a skipped one.
    """
    accounts = []
    for ch in children:
        fhsa_room = ch.get('fhsa_room_accumulated', 0.0)
        accounts.append({
            'rrsp_balance': ch.get('rrsp_balance', 0.0),
            'tfsa_balance': ch.get('tfsa_balance', 0.0),
            'fhsa_balance': ch.get('fhsa_balance', 0.0),
            'non_reg_balance': 0.0,
            # epic #841 bite 4: the child non-reg account's adjusted cost base,
            # so the family objective can tax its accrued gain at deemed
            # disposition on the SAME footing as the adults' non-reg (DP#19:
            # track cost basis from day one). Opens at 0.0 alongside the
            # balance; each year's contribution adds to it, growth does not.
            'non_reg_acb': 0.0,
            # Issue #859 (Part A): cumulative principal an intra-family LOAN
            # (a repayable gift) has funded into this child's registered room.
            # Opens at 0.0; each year's loan-kind funding adds to it. It is the
            # child's LIABILITY (owed to the lender) and the mirror of the
            # lender's RECEIVABLE on the family balance sheet -- so a funding
            # loan does not reduce the lender's net worth (DP#18). Zero for a
            # child funded only by their own income and/or plain gifts.
            'loan_funded_principal': 0.0,
            'rrsp_room': ch.get('rrsp_room_accumulated', 0.0),
            'tfsa_room': ch.get('tfsa_room_accumulated', 0.0),
            'fhsa_room': fhsa_room,
            # DP#1/#701: FHSA lifetime cap from the contract when declared;
            # absent, the annual accumulated room is the binding cap (do not
            # invent a larger lifetime the child may not have) -- same rule
            # scenario_discovery._discover_child_accounts already applies.
            'fhsa_lifetime_remaining': ch.get('fhsa_lifetime_limit', fhsa_room),
        })
    return accounts


def child_savings_for_year(children: list, savings_rate: float,
                           salary_growth: float, year: int) -> list:
    """Each child's OWN incremental savings this year: the child's OWN grown
    gross income times the household savings rate (epic #841 bite 2, #812).

    Parallel to ``config.children`` by index. Uses the SAME salary-growth curve
    the prologue already applies when it adds child income to ``total_income``.
    0.0 for a child with no declared income -- a modelled zero (the entry
    exists), not a defaulted split (DP#32). The prologue carves this sum out of
    the adult allocation base so a child's dollars fund the child's OWN accounts
    rather than being routed into the household pot twice (DP#18: money is
    redirected, never created).
    """
    return [
        ch.get('gross_income', 0) * (1 + salary_growth) ** year * savings_rate
        for ch in children
    ]


def child_after_tax_savings_for_year(children: list, savings_rate: float,
                                     salary_growth: float, year: int,
                                     brackets: list) -> list:
    """Each child's OWN AFTER-TAX savings this year (issue #701 Step 6 of #643).

    A child is a SEPARATE taxpayer: Canada has no joint filing, so a child's own
    income is taxed on the CHILD's own return in the CHILD's own bracket -- not
    folded into a parent's return at the parent's (higher) marginal rate. This
    runs each child's OWN grown income through ``tax_on_income`` at that same
    child-level income and funds the child's OWN accounts from the AFTER-TAX
    remainder (``(gross - tax) * savings_rate``), not the gross the pre-Step-6
    fold used.

    Parallel to ``config.children`` by index, using the SAME salary-growth curve
    and the SAME year-versioned combined ``brackets`` the adult tax loop uses
    (``_income_tax_by_adult``) -- one tax spelling for every member (DP#9). A
    child with no income keeps ``tax_on_income(0) == 0``, so their after-tax
    savings stay a MODELLED 0.0 (DP#32), byte-identical to bite 2 -- which is why
    the golden household (children earn 0) is unchanged.
    """
    from tax_calculator import tax_on_income
    out = []
    for ch in children:
        grown = ch.get('gross_income', 0) * (1 + salary_growth) ** year
        after_tax = grown - tax_on_income(grown, brackets)
        out.append(after_tax * savings_rate)
    return out


def _child_registered_room_after_own(prior: dict, own: float) -> float:
    """The registered room a cross-member transfer can still fill for one child
    this year: the child's remaining TFSA + (room-capped) FHSA + RRSP room, LESS
    the child's own savings which take their share first. >= 0. Spelled ONCE and
    reused by both ``child_gift_funding_for_year`` and
    ``child_loan_funded_for_year`` so gifts and loans are capped identically
    (DP#9), and by the child's ACCRUED room (#857) via ``prior``.
    """
    fhsa_cap = max(0.0, min(prior['fhsa_room'], prior['fhsa_lifetime_remaining']))
    registered_room = (max(0.0, prior['tfsa_room'])
                       + fhsa_cap
                       + max(0.0, prior['rrsp_room']))
    return max(0.0, registered_room - own)


def child_gift_funding_for_year(children: list, gifts: list,
                                child_own_savings: list,
                                prior_child_accounts: list) -> list:
    """Each child's parent->child GIFT funding this year (epic #841 bite 3).

    Parallel to ``config.children`` by index. A gift is a parent moving
    after-tax money to a child so the child's OWN registered room gets filled
    beyond what the child's small income alone can fund. The result is capped so
    it fills ONLY the child's remaining REGISTERED room this year (TFSA + FHSA +
    RRSP, from ``prior_child_accounts`` -- the state that ``_step_child_accounts``
    grows), AFTER the child's own savings have taken their share of that room:

        effective_gift = min(declared_gift,
                             max(0, registered_room - child_own_savings))

    This makes the gift SELF-LIMITING (once the room is full it funds nothing
    further) and keeps it out of non-registered accounts (asset location is a
    later scope). The caller carves this exact amount out of the ADULT
    allocation base (DP#18: the money is REDIRECTED from the parent to the
    child, never created -- the household adult pot drops by precisely what
    lands in the child's registered accounts) and passes it back into the fold,
    so the cap is spelled ONCE (DP#9).

    A gift is NOT income to the child and NOT deductible to the donor (an
    inter-vivos cash gift is tax-free to both), so this touches no tax term --
    only the after-tax savings routing. Returns all-zeros when no gifts are
    declared (the golden household), leaving the adult base bit-identical.
    """
    if not gifts:
        return [0.0 for _ in children]
    by_child: Dict[str, float] = {}
    for g in gifts:
        by_child[g['to']] = by_child.get(g['to'], 0.0) + float(g['amount'])
    out = []
    for i, ch in enumerate(children):
        declared = by_child.get(ch.get('id'), 0.0)
        if declared <= 0.0 or i >= len(prior_child_accounts):
            out.append(0.0)
            continue
        prior = prior_child_accounts[i]
        own = child_own_savings[i] if i < len(child_own_savings) else 0.0
        out.append(min(declared, _child_registered_room_after_own(prior, own)))
    return out


def child_loan_funded_for_year(children: list, gifts: list,
                               child_own_savings: list,
                               prior_child_accounts: list) -> list:
    """Each child's REPAYABLE-gift (intra-family LOAN) funding this year, the
    portion of ``child_gift_funding_for_year`` attributable to loans (issue #859
    Part A).

    A repayable gift (``repayable: True``) funds the child's registered room
    exactly like a plain gift -- the caller carves the SAME total (all transfers,
    loan + gift) out of the adult base and grows the child's accounts by it, so
    the child's balances are bit-identical whether a transfer is a loan or a gift
    (DP#9: one cap, ``child_gift_funding_for_year``). What differs is the BALANCE
    SHEET: the loan principal that lands is a RECEIVABLE the donor keeps and a
    LIABILITY the child owes. This function returns just that loan-kind principal
    per child so the fold can accumulate it onto ``loan_funded_principal``.

    Loans are funded FIRST within the child's remaining room (then plain gifts
    fill whatever room is left), so::

        loan_funded  = min(declared_loans, room_after_own_savings)

    and ``loan_funded + gift_funded == child_gift_funding_for_year`` exactly (the
    identity ``min(a, R) + min(b, R - min(a, R)) == min(a + b, R)``): no dollar
    is double-counted or lost. The cap uses the child's remaining ACCRUED room
    (#857), so the receivable a loan can build is self-limited by the room the
    child actually has. All-zeros when no repayable gift is declared (a plain
    gift, or the golden household) -- a modelled zero, never a fabricated debt
    (DP#32).
    """
    if not gifts:
        return [0.0 for _ in children]
    loans_by_child: Dict[str, float] = {}
    for g in gifts:
        if not g.get('repayable', False):
            continue
        loans_by_child[g['to']] = loans_by_child.get(g['to'], 0.0) + float(g['amount'])
    if not loans_by_child:
        return [0.0 for _ in children]
    out = []
    for i, ch in enumerate(children):
        declared = loans_by_child.get(ch.get('id'), 0.0)
        if declared <= 0.0 or i >= len(prior_child_accounts):
            out.append(0.0)
            continue
        prior = prior_child_accounts[i]
        own = child_own_savings[i] if i < len(child_own_savings) else 0.0
        out.append(min(declared, _child_registered_room_after_own(prior, own)))
    return out


def child_room_accrual_for_year(children: list, calendar_year: int,
                                start_year: int, salary_growth: float,
                                year: int, rrsp_annual_limit: float,
                                tfsa_annual_limit: float,
                                rrsp_annual_percent: float) -> Tuple[list, list]:
    """Each child's OWN registered room accrued THIS year (issue #857).

    A child's contribution room is not static: it ACCRUES year over year exactly
    as an adult's does. Returns ``(tfsa_accruals, rrsp_accruals)`` parallel to
    ``config.children`` by index, for ``_step_child_accounts`` to add onto the
    carried-forward room after this year's contributions have decremented it.

    * TFSA room begins the year the child turns 18 and accrues the year's TFSA
      dollar limit each year thereafter. Age is computed from ``birth_year``
      (DP#1), falling back to the declared ``age`` at ``start_year`` when no
      birth year is given -- the SAME age derivation the RESP rule uses. A child
      younger than 18 (or with no age at all) accrues 0.0 (DP#32: a modelled
      zero, not a skipped child) -- the no-op the golden household relies on.
    * RRSP room accrues the statutory 18% (``rrsp_annual_percent``, ITA
      s.146(1)) of the child's OWN earned income -- the child's grown gross
      income, on the SAME salary-growth curve ``child_savings_for_year`` uses --
      capped at the year's RRSP dollar limit. This is the SAME formula the adult
      path spells (``apply_contribution_room`` / ``step_extra_adult_accounts``),
      extended to children rather than duplicated (DP#9). RRSP room accrues from
      earned income at any age (a working minor accrues room); the 18-year gate
      is TFSA-only.
    """
    tfsa_accruals = []
    rrsp_accruals = []
    for ch in children:
        birth_year = ch.get('birth_year', 0)
        if not birth_year:
            age0 = ch.get('age', 0)
            birth_year = (start_year - age0) if age0 else 0
        age = (calendar_year - birth_year) if birth_year else 0
        tfsa_accruals.append(tfsa_annual_limit if age >= 18 else 0.0)
        earned = ch.get('gross_income', 0) * (1 + salary_growth) ** year
        rrsp_accruals.append(min(rrsp_annual_percent * earned, rrsp_annual_limit))
    return tfsa_accruals, rrsp_accruals


def _step_child_accounts(prior_accounts: list, child_savings: list,
                         child_pcts: dict, investment_return: float,
                         child_gift_amounts: Optional[list] = None,
                         tfsa_room_accrual: Optional[list] = None,
                         rrsp_room_accrual: Optional[list] = None,
                         child_loan_amounts: Optional[list] = None) -> list:
    """Grow each child's OWN accounts by one projection year (epic #841 bite 2).

    Pure step (DP#26): the child's OWN savings are routed by
    ``StrategyEngine.allocate_child`` -- by ROOM, never by a deduction the child
    does not get (#701) -- into the child's OWN TFSA/FHSA/RRSP/non-reg; each
    account then compounds at ``investment_return`` and its room is decremented
    by the contribution and carried forward. The routing honours the strategy's
    declared ``child_*_pct`` targets (the contract's opinion, swept by
    scenario_discovery); absent a declared target each pct is 0.0 and
    allocate_child falls back to its #701 room-priority waterfall (TFSA -> FHSA
    -> RRSP -> non-reg) -- a fallback, not a hardcoded opinion (DP#13).

    Issue #857: after the contribution decrements the room, this year's room
    ACCRUAL is added on -- ``tfsa_room_accrual``/``rrsp_room_accrual``, computed
    once by ``child_room_accrual_for_year`` (TFSA opens at 18; RRSP accrues 18%
    of the child's earned income) and passed in parallel by index. This mirrors
    the adult path (room decremented, then re-accrued) exactly (DP#9). When
    absent (a direct unit-test caller / the year-0 pre-step), each accrual is
    0.0 -- room is carried forward decrement-only, byte-identical to bite 2.

    A child with no savings AND no room yields an all-zero step -- the loop
    still runs for that child and produces the modelled zero, it is not skipped
    (DP#32).
    """
    from strategy import AllocationStrategy, StrategyEngine, ChildState
    strategy = AllocationStrategy(
        child_tfsa_pct=child_pcts.get('tfsa', 0.0),
        child_fhsa_pct=child_pcts.get('fhsa', 0.0),
        child_rrsp_pct=child_pcts.get('rrsp', 0.0),
        child_non_reg_pct=child_pcts.get('non_reg', 0.0),
    )
    engine = StrategyEngine(strategy)
    r = 1 + investment_return
    new_accounts = []
    for i, prior in enumerate(prior_accounts):
        savings = child_savings[i] if i < len(child_savings) else 0.0
        # Epic #841 bite 3: a parent->child gift ADDS to the child's own savings
        # for this year, funding more of the child's registered room than the
        # child's income alone could. The amount was already capped to the
        # child's remaining registered room by child_gift_funding_for_year (one
        # spelling of the cap, DP#9), so the combined savings stays inside the
        # room and allocate_child routes it all into registered accounts (never
        # non-reg). Absent gifts -> 0.0, leaving the child's own routing intact.
        if child_gift_amounts is not None and i < len(child_gift_amounts):
            savings += child_gift_amounts[i]
        # Issue #859 (Part A): the LOAN-kind portion of this year's funding (a
        # subset of child_gift_amounts -- the caller carves the same total).
        # It does NOT change the child's account growth (already added above via
        # child_gift_amounts); it only ACCUMULATES onto loan_funded_principal --
        # the child's LIABILITY / the lender's RECEIVABLE on the family balance
        # sheet (DP#18). Absent -> 0.0, so loan_funded_principal stays put.
        loan_amount = (child_loan_amounts[i]
                       if child_loan_amounts is not None
                       and i < len(child_loan_amounts) else 0.0)
        # Issue #857: this year's room accrual, added AFTER the contribution
        # decrements the carried-forward room (same order as the adult path).
        tfsa_accrual = (tfsa_room_accrual[i]
                        if tfsa_room_accrual is not None
                        and i < len(tfsa_room_accrual) else 0.0)
        rrsp_accrual = (rrsp_room_accrual[i]
                        if rrsp_room_accrual is not None
                        and i < len(rrsp_room_accrual) else 0.0)
        routing = engine.allocate_child(ChildState(
            savings=savings,
            tfsa_room=prior['tfsa_room'],
            fhsa_room=prior['fhsa_room'],
            fhsa_lifetime_remaining=prior['fhsa_lifetime_remaining'],
            rrsp_room=prior['rrsp_room'],
        ))
        new = {
            'rrsp_balance': (prior['rrsp_balance'] + routing.rrsp) * r,
            'tfsa_balance': (prior['tfsa_balance'] + routing.tfsa) * r,
            'fhsa_balance': (prior['fhsa_balance'] + routing.fhsa) * r,
            'non_reg_balance': (prior['non_reg_balance'] + routing.non_reg) * r,
            # epic #841 bite 4: ACB grows by the CONTRIBUTION only (cost basis),
            # never by the year's growth -- the divergence between this and
            # non_reg_balance IS the accrued gain the family objective taxes at
            # deemed disposition (DP#19). .get keeps a child account dict seeded
            # before bite 4 (no acb key) working -- absence is opening 0.0.
            'non_reg_acb': prior.get('non_reg_acb', 0.0) + routing.non_reg,
            # Issue #859 (Part A): carry the child's cumulative loan liability
            # forward and add this year's loan-funded principal. .get keeps a
            # dict seeded before this issue working (absence is opening 0.0).
            'loan_funded_principal': prior.get('loan_funded_principal', 0.0) + loan_amount,
            'rrsp_room': max(0.0, prior['rrsp_room'] - routing.rrsp) + rrsp_accrual,
            'tfsa_room': max(0.0, prior['tfsa_room'] - routing.tfsa) + tfsa_accrual,
            'fhsa_room': max(0.0, prior['fhsa_room'] - routing.fhsa),
            'fhsa_lifetime_remaining': max(0.0, prior['fhsa_lifetime_remaining'] - routing.fhsa),
        }
        # Issue #704: carry an already-opened HBP (Home Buyers' Plan) forward
        # untouched through the growth step -- its RRSP withdrawal and 15-year
        # repayment schedule are stepped separately by
        # ``apply_child_first_home_purchases`` after this pass. Present ONLY once
        # a first-home purchase has fired for this child; absent otherwise, so a
        # non-buyer's account dict is byte-identical to bite 2 (DP#32).
        if prior.get('hbp') is not None:
            new['hbp'] = prior['hbp']
        new_accounts.append(new)
    return new_accounts


def apply_child_first_home_purchases(accounts: list, children: list,
                                     first_home_purchases: list,
                                     calendar_year: int) -> list:
    """Wire a child's first-home FHSA qualifying withdrawal + HBP RRSP withdrawal
    into the fold (issue #704), plus each subsequent year's HBP repayment.

    A child who becomes a first-time home buyer uses the two instruments Canada
    provides for a first home, calling the correctly-built (but previously
    orphaned) ``countries.canada.fhsa`` / ``countries.canada.hbp_rules`` modules
    (DP#9 -- the rules live in one place):

    * **FHSA qualifying withdrawal** (``FHSAAccount.qualifying_withdrawal``): the
      child's whole FHSA balance is withdrawn TAX-FREE for the first home and the
      account closes. A child holding an FHSA has no prior principal residence,
      so the withdrawal always qualifies.
    * **HBP** (``HBPAccount``): up to ``HBP_MAX_WITHDRAWAL`` ($60,000) is
      withdrawn from the child's own RRSP NON-TAXABLY, and the 15-year repayment
      schedule is generated and tracked on the account.

    Both amounts land in the child's OWN cash (``non_reg_balance``) as the home
    down payment, with cost basis set equal (a withdrawal to cash carries no
    accrued gain), so the child's net worth is CONSERVED at the purchase (money
    is moved between the child's own pots, never created or destroyed -- DP#18):
    the FHSA drains tax-free, and the HBP is an interest-free loan the child owes
    back to their own RRSP.

    Each year on/after the repayment start year, that year's scheduled repayment
    is moved from the child's cash back into their RRSP (again net-worth-neutral,
    and an HBP repayment consumes no RRSP contribution room), the outstanding
    balance decreasing until the plan is repaid.

    ``first_home_purchases`` is a list of ``{'buyer': <member id>, 'year': int}``.
    Parallel to ``config.children`` by index via each child's ``id``. Absent (the
    golden household declares none) this returns ``accounts`` UNCHANGED -- a
    genuine no-op, so the golden invariant cannot move (DP#32).
    """
    if not first_home_purchases:
        return accounts
    buyers_this_year = {p['buyer'] for p in first_home_purchases
                        if int(p['year']) == calendar_year}
    out = []
    for i, acc in enumerate(accounts):
        child_id = children[i].get('id') if i < len(children) else None
        out.append(_apply_first_home_to_account(
            acc, child_id is not None and child_id in buyers_this_year,
            calendar_year))
    return out


def _apply_first_home_to_account(acc: dict, buys_this_year: bool,
                                 calendar_year: int) -> dict:
    """One member's first-home step on a SINGLE account dict (issue #704/#931).

    Shared verbatim by the child fold (``apply_child_first_home_purchases``, one
    dict per child) and the adult fold (``apply_adult_first_home_purchases``, a
    synthetic dict over the buyer's household FHSA + own RRSP + household cash),
    so the two first-home instruments live in exactly ONE place (DP#9). ``acc``
    carries the member's pots as flat keys ``fhsa_balance`` /
    ``fhsa_lifetime_remaining`` / ``rrsp_balance`` / ``non_reg_balance`` /
    ``non_reg_acb`` and an optional ``hbp`` record; a NEW dict is returned (the
    input is never mutated).

    * When ``buys_this_year`` the FHSA qualifying withdrawal drains the balance
      TAX-FREE (the account closes) and the HBP withdraws ``min(RRSP, $60k)``
      NON-TAXABLY, both landing in ``non_reg`` cash as the down payment (net worth
      conserved -- the HBP is an interest-free loan owed back to the RRSP).
    * Every year an OPEN HBP repays that year's scheduled amount (RRSP<-cash),
      whether it opened this year or a prior one -- a no-op until the repayment
      window (the 3rd year after withdrawal).
    """
    from countries.canada.fhsa import FHSAAccount
    from countries.canada.hbp_rules import HBPAccount, HBP_MAX_WITHDRAWAL
    new = dict(acc)
    # 1. A purchase FIRING this year opens the FHSA withdrawal + HBP.
    if buys_this_year:
        fhsa = FHSAAccount(balance=acc['fhsa_balance'], open_year=calendar_year)
        fhsa_result = fhsa.qualifying_withdrawal(calendar_year)
        fhsa_out = fhsa_result['amount'] if fhsa_result['eligible'] else 0.0
        hbp_out = min(acc['rrsp_balance'], HBP_MAX_WITHDRAWAL)
        hbp = HBPAccount(withdrawal=hbp_out, withdrawal_year=calendar_year)
        schedule = hbp.generate_repayment_schedule()
        down_payment = fhsa_out + hbp_out
        new['fhsa_balance'] = fhsa.balance  # 0 after a qualifying withdrawal
        new['fhsa_lifetime_remaining'] = 0.0  # the account closes
        new['rrsp_balance'] = acc['rrsp_balance'] - hbp_out
        new['non_reg_balance'] = acc['non_reg_balance'] + down_payment
        new['non_reg_acb'] = acc.get('non_reg_acb', 0.0) + down_payment
        new['hbp'] = {
            'withdrawal': hbp_out,
            'withdrawal_year': calendar_year,
            'repaid': 0.0,
            'outstanding': hbp.outstanding,
            'repayment_schedule': schedule,
        }
    # 2. An OPEN HBP repays this year's scheduled amount (RRSP<-cash), whether
    #    it opened this year or a prior one. No-op until the repayment window.
    hbp_state = new.get('hbp')
    if hbp_state is not None and hbp_state['outstanding'] > 0.0:
        due = sum(row['actual_payment']
                  for row in hbp_state['repayment_schedule']
                  if row['year'] == calendar_year)
        payment = min(due, new['non_reg_balance'], hbp_state['outstanding'])
        if payment > 0.0:
            new['non_reg_balance'] -= payment
            new['non_reg_acb'] = max(0.0, new.get('non_reg_acb', 0.0) - payment)
            new['rrsp_balance'] += payment
            new['hbp'] = {**hbp_state,
                          'repaid': hbp_state['repaid'] + payment,
                          'outstanding': max(0.0, hbp_state['outstanding'] - payment)}
    return new


def apply_adult_first_home_purchases(prior_adult_hbp: dict,
                                     first_home_purchases: list,
                                     adult_ids: list, *,
                                     fhsa_balance: float,
                                     rrsp_by_slot: dict,
                                     non_reg_balance: float,
                                     non_reg_acb: float,
                                     calendar_year: int):
    """Fire each ADULT first-time home buyer's FHSA + HBP withdrawals and repay
    any open adult HBP (issue #931) -- the adult analogue of
    ``apply_child_first_home_purchases``, sharing the same per-account step
    (``_apply_first_home_to_account``, DP#9).

    The engine models ONE household FHSA (slot 0), so an adult buyer's FHSA
    qualifying withdrawal drains that household pot; the HBP comes from the
    buyer's OWN RRSP slot (0=primary, 1=spouse) so each spouse can withdraw up to
    their own $60k. The down payment lands in the household non-registered cash
    (all three pots are counted in ``total_assets``, so net worth is conserved).

    Pure (DP#26): mutates nothing. ``adult_ids`` is the adult member ids in
    canonical order (primary first); its index is the RRSP slot. ``rrsp_by_slot``
    is ``{0: primary_own, 1: spouse_own}``. ``prior_adult_hbp`` is
    ``{buyer_id: {'slot': int, ...hbp record...}}`` carried from the prior year.
    Returns ``(fhsa_balance, rrsp_by_slot, non_reg_balance, non_reg_acb,
    new_adult_hbp, fhsa_closed)`` where ``fhsa_closed`` is True when a purchase
    drained the household FHSA this year (the caller closes its room).

    Absent any declared purchase AND any carried HBP this returns the inputs
    UNCHANGED (the golden household) -- a genuine no-op, so the invariant cannot
    move (DP#32).
    """
    slot_of = {aid: i for i, aid in enumerate(adult_ids)}
    buyers_this_year = {p['buyer'] for p in first_home_purchases
                        if int(p['year']) == calendar_year and p['buyer'] in slot_of}
    # Every adult that either buys this year or is repaying a prior HBP needs a
    # step; a buyer that is a CHILD (not in slot_of) is handled by the child fold.
    active_ids = buyers_this_year | set(prior_adult_hbp)
    rrsp_by_slot = dict(rrsp_by_slot)
    new_adult_hbp: dict = {}
    fhsa_closed = False
    for aid in active_ids:
        slot = slot_of[aid]
        buys = aid in buyers_this_year
        acc = {
            # The FHSA qualifying withdrawal always drains the single household
            # FHSA (slot 0); a repay-only step never touches it.
            'fhsa_balance': fhsa_balance if buys else 0.0,
            'rrsp_balance': rrsp_by_slot[slot],
            'non_reg_balance': non_reg_balance,
            'non_reg_acb': non_reg_acb,
        }
        if aid in prior_adult_hbp:
            acc['hbp'] = {k: v for k, v in prior_adult_hbp[aid].items()
                          if k != 'slot'}
        new = _apply_first_home_to_account(acc, buys, calendar_year)
        if buys:
            fhsa_balance = new['fhsa_balance']
            fhsa_closed = True
        rrsp_by_slot[slot] = new['rrsp_balance']
        non_reg_balance = new['non_reg_balance']
        non_reg_acb = new['non_reg_acb']
        if new.get('hbp') is not None:
            new_adult_hbp[aid] = {'slot': slot, **new['hbp']}
    return (fhsa_balance, rrsp_by_slot, non_reg_balance, non_reg_acb,
            new_adult_hbp, fhsa_closed)


def step_extra_adult_accounts(prior_adult_rrsp: dict, prior_adult_tfsa: dict,
                              extra_specs: list, investment_return: float,
                              rrsp_annual_limit: float,
                              tfsa_annual_limit: float) -> list:
    """Grow each ADDITIONAL accumulating adult's OWN RRSP/TFSA by one projection
    year (issue #899 part a).

    ``extra_specs`` is one dict per adult beyond the primary couple (the store
    slots >= 2)::

        {'id': <stable entity id>, 'earned_income': float, 'savings': float}

    where ``savings`` is that adult's OWN after-tax income available to invest
    this year (already net of their INDIVIDUALLY computed tax -- Canada has no
    joint filing) and ``earned_income`` drives RRSP room re-accrual (ITA
    s.146(1), 18%).

    Pure step (DP#26), mirroring ``_step_child_accounts``: the adult's savings
    fill their OWN RRSP up to room (capped by the year's dollar limit), then
    their OWN TFSA up to room; each account compounds at ``investment_return``;
    room is decremented by the contribution and re-accrued (RRSP: 18% of the
    year's earned income, capped at the annual limit; TFSA: the annual limit).

    This is a SEPARATE, ACCUMULATION-ONLY path from the primary couple's
    two-slot compute -- an extra adult is admitted only when they never
    decumulate across the horizon (``input_contract``'s admission gate; a
    retired / benefit-drawing extra is refused to #901), so no drawdown / RRIF /
    CPP / OAS mechanics apply here. Full consolidation of this path with the
    two-slot contribution/growth rules is deferred with the rest of the N-adult
    decumulation model (#901).

    Returns one end-of-year dict per spec (``rrsp_balance``/``rrsp_room``/
    ``tfsa_balance``/``tfsa_room`` plus the ``earned_income``/``savings`` used).
    Empty for a two-adult household (no extra specs) -> the caller writes
    nothing back -> byte-identical.
    """
    r = 1 + investment_return
    out = []
    for spec in extra_specs:
        aid = spec['id']
        rrsp_e = prior_adult_rrsp.get(aid, {})
        tfsa_e = prior_adult_tfsa.get(aid, {})
        rrsp_bal = rrsp_e.get('own', 0.0)
        rrsp_room = rrsp_e.get('own_room', 0.0)
        tfsa_bal = tfsa_e.get('balance', 0.0)
        tfsa_room = tfsa_e.get('room', 0.0)
        savings = max(0.0, spec['savings'])
        rrsp_contrib = min(savings, rrsp_room, rrsp_annual_limit)
        savings -= rrsp_contrib
        tfsa_contrib = min(savings, tfsa_room, tfsa_annual_limit)
        earned = max(0.0, spec['earned_income'])
        rrsp_room_next = max(0.0, rrsp_room - rrsp_contrib) + min(0.18 * earned, rrsp_annual_limit)
        tfsa_room_next = max(0.0, tfsa_room - tfsa_contrib) + tfsa_annual_limit
        out.append({
            'id': aid,
            'rrsp_balance': (rrsp_bal + rrsp_contrib) * r,
            'rrsp_room': rrsp_room_next,
            'tfsa_balance': (tfsa_bal + tfsa_contrib) * r,
            'tfsa_room': tfsa_room_next,
            'earned_income': earned,
            'savings': spec['savings'],
        })
    return out


def _default_heloc_tracing() -> dict:
    """Return a fresh HELOC tracing dict with zero values."""
    return {
        'total_advances': 0.0,
        'investment_advances': 0.0,
        'rrsp_advances': 0.0,
        'tfsa_advances': 0.0,
        'personal_draws': 0.0,
    }


# ── Emergency-reserve carve-out helpers (issue #688) ────────────────────────
# The reserve is a cash sleeve held INSIDE a named account. These map the
# declared `held_in` account kind onto the balance the sleeve is carved from.
# Keys are account kinds, not account ids: input_contract.py resolves the
# declared `accounts[].id` to the kind the engine actually tracks, because
# this engine holds ONE pot per kind (#643), not one per account.

# Issue #700/#643/#704: a reserve held in a registered account is carved from
# the relevant per-adult store, not a flat key. Maps the held_in kind to (store
# key, adult slot, balance field): 'rrsp' is the primary adult's OWN RRSP;
# 'tfsa' / 'tfsa_spouse' are the primary / spouse TFSA; 'fhsa' is the primary
# adult's FHSA (slot 0) -- exactly the accounts the old flat rrsp_balance /
# tfsa_primary_balance / tfsa_spouse_balance / fhsa_balance named.
_RESERVE_HOST_ADULT_STORE = {
    'rrsp': ('adult_rrsp', 0, 'own'),
    'tfsa': ('adult_tfsa', 0, 'balance'),
    'tfsa_spouse': ('adult_tfsa', 1, 'balance'),
    'fhsa': ('adult_fhsa', 0, 'balance'),
}

# Every held_in kind this engine can carve a reserve from (for the loud error).
_RESERVE_HOST_KINDS = set(_RESERVE_HOST_ADULT_STORE) | {'non_reg'}


def _annual_debt_service(config: SimulationConfig) -> float:
    """This household's annual mortgage payment -- the debt-service half of
    the "essential outflows" the reserve target is sized against (#688).

    Reuses ``countries.canada.rate_model.monthly_payment`` (the standard
    annuity formula the engine already amortizes with) rather than
    re-deriving it here: two copies of an amortization formula is exactly
    the duplication the clone detector exists to catch, and a reserve sized
    against a payment that disagrees with the one the engine actually
    charges would be wrong in a way no test would see. Imported lazily,
    matching this module's existing DP#25 pattern (see
    ``_canada_fhsa_limits`` above) -- an annuity is not a Canadian tax rule,
    but its one implementation happens to live there today.
    """
    if config.mortgage_balance <= 0 or config.amortization_years <= 0:
        return 0.0
    from countries.canada.rate_model import monthly_payment
    return monthly_payment(
        config.mortgage_balance, config.mortgage_rate, config.amortization_years) * 12


def _annual_consumer_loan_service(config: SimulationConfig) -> float:
    """The year-0 annual payment on the household's closed-end consumer loans
    (issue #763) -- the consumer-debt half of the "essential outflows" the
    #758 reserve/runway target is sized against, alongside the mortgage's
    ``_annual_debt_service``.

    Year 0 is the only year every consumer loan is still active (each
    amortizes to 0 at its own payoff date), so this is the MAXIMUM annual
    consumer-debt service -- the right figure for sizing a reserve meant to
    cover a shortfall at the start of the projection. Later years' payments
    decline as loans pay off (computed in the fold by
    simulation_rules.apply_consumer_loans); the reserve target is re-sized
    every year off that rule's output, not off this helper.

    Uses each loan's DECLARED ``payment_monthly`` (a contract fact, not a
    re-derived annuity) -- duplicating the amortization formula here would
    be exactly the clone the detector catches, and a reserve sized against a
    payment that disagreed with the one the engine charges would be wrong in
    a way no test would see (same reasoning as ``_annual_debt_service``).
    """
    return sum(loan['payment_monthly'] * 12 for loan in config.consumer_loans)


def _annual_installment_service(config: SimulationConfig) -> float:
    """The year-0 annual payment on the household's fixed-term installment
    obligations (issue #759) -- the installment half of the "essential
    outflows" the #758 reserve/runway target is sized against, alongside the
    mortgage's ``_annual_debt_service`` and the consumer loans'
    ``_annual_consumer_loan_service``.

    Year 0 is the MAXIMUM installment service only when the plan is already
    active in year 0 (start_date in the simulation's start_year). A plan that
    starts in a LATER year pays nothing in year 0 -- so this helper sizes the
    reserve against the installment outflows the household faces AT THE START
    of the projection, which is the right figure for a reserve meant to cover
    a shortfall at the start (later years' payments are re-sized every year
    in the fold by apply_solvency, off apply_installments' output, not off
    this helper -- the same split ``_annual_consumer_loan_service`` makes).

    Uses each plan's DECLARED ``monthly_amount`` and the count of its payment
    dates falling in the simulation's start_year (a contract fact + a
    date-count, not a re-derived annuity) -- duplicating the date-scheduled
    payment math here would be exactly the clone the detector catches, so the
    shared ``_installment_payment_in_year`` helper in simulation_rules is the
    one spelling of that count (imported lazily to mirror this module's
    DP#25 pattern for the amortization helper above).
    """
    if not config.installments:
        return 0.0
    from rules_debt import _installment_payment_in_year
    return sum(
        _installment_payment_in_year(plan, config.start_year)
        for plan in config.installments)


def _host_account_balance(held_in: str, canada_state: dict, non_reg_balance: float) -> float:
    """The opening balance of the account the reserve is declared to live in."""
    if held_in == 'non_reg':
        return non_reg_balance
    store_ref = _RESERVE_HOST_ADULT_STORE.get(held_in)
    if store_ref is None:
        raise ValueError(
            f"assumptions.emergency_reserve.held_in resolved to account kind "
            f"{held_in!r}, which this engine has no balance for. A reserve is a "
            f"cash sleeve carved out of a real account (#688); it cannot be held "
            f"in an account the simulation does not track. Supported: "
            f"{sorted(_RESERVE_HOST_KINDS)}, or null "
            f"(held outside every declared account)."
        )
    store_key, index, bal_field = store_ref
    entries = list(canada_state.get(store_key, {}).values())
    return entries[index][bal_field] if index < len(entries) else 0.0


def _carve_from_canada(held_in: str, canada_state: dict, amount: float) -> None:
    """Subtract the reserve sleeve from its Canada-side host account, in
    place on the freshly-built ``canada_state`` dict (never on a shared
    one -- ``SimState.initial`` constructs it immediately above the call).
    Every host is a per-adult store (#700/#643/#704)."""
    store_key, index, bal_field = _RESERVE_HOST_ADULT_STORE[held_in]
    entries = list(canada_state.get(store_key, {}).values())
    if index < len(entries):
        entries[index][bal_field] -= amount


def _property_equity_for_year(prop: Dict, cal_year: int, start_year: int) -> float:
    """Issue #696 (epic #690 bite 5): a non-principal property's net-equity
    contribution to the balance sheet in calendar ``cal_year``.

    A property with a dated mid-horizon purchase (``prop['purchase']['year']``,
    mapped by ``contract_property._map_owned_properties``) is NOT yet owned before
    that year, so it contributes ZERO equity until then and its full
    ``net_equity`` from the purchase year onward -- the mortgage originates and
    the equity enters the sheet in the same year the down payment leaves cash.
    A property with no ``purchase`` is held from year 0 (the #692 static
    figure).

    Issue #956 bite A -- APPRECIATION: when the property declares an
    ``appreciation_rate`` (a real annual growth rate, e.g. 0.03), its GROSS
    value compounds at that rate from the year ownership begins (the purchase
    year for a dated purchase, else ``start_year``), and the equity is the
    appreciated value less the secured mortgage. Absence-safe (DP#32): an absent
    or 0.0 rate returns the static #692 ``net_equity`` unchanged and never reads
    ``value_share``/``secured_share`` -- so a household that declares no
    appreciation (incl. the golden fixture) is byte-identical to today. In the
    ownership year itself (``years_held == 0``) the appreciated value equals the
    gross value, so equity equals ``net_equity`` -- appreciation accrues only
    from the NEXT year, and the purchase-year down payment (= ``net_equity``) is
    unaffected. The secured mortgage is held at its declared snapshot this bite
    (amortization is a follow-up lever); appreciation -- the dominant driver of
    real-estate TIMING -- is what bite A adds.

    Issue #956 bite B -- SALE: a property with a dated mid-horizon sale
    (``prop['sale']['year']``, mapped by ``contract_property._map_owned_properties``)
    contributes ZERO equity from its sale year ONWARD (``cal_year >= sale_year``);
    in the sale year the property leaves the balance sheet and its net proceeds
    replace it in the portfolio (invested by the sale-year handler in the tax +
    proceeds layer built on top of this bite). The sale gate is placed AFTER the
    purchase gate and BEFORE the appreciation branch so it WINS over both: a
    sold property has no equity regardless of appreciation. Absence-safe (DP#32):
    a property with no ``sale`` is held to the horizon and the gate never fires,
    byte-identical to #692/#696."""
    purchase = prop.get('purchase')
    if purchase is not None and cal_year < purchase['year']:
        return 0.0
    sale = prop.get('sale')
    if sale is not None and cal_year >= sale['year']:
        return 0.0
    rate = prop.get('appreciation_rate')
    if rate is None or rate == 0.0:
        return prop['net_equity']
    owned_from = purchase['year'] if purchase is not None else start_year
    years_held = max(0, cal_year - owned_from)
    appreciated_value = prop['value_share'] * ((1.0 + rate) ** years_held)
    return appreciated_value - prop['secured_share']


@dataclass
class SimState:
    """Explicit simulation state — DP#26: the data object returned by simulate_year.

    Every mutable value that _simulate_year reads or writes is captured here.
    This makes simulate_year a pure function: same (state, action, config, return_model)
    always yields the same (YearResult, next_state).

    SimState is copyable via dataclasses.replace(), enabling optimizer modes
    to fork state at any year for parallel exploration (DP, scipy, Monte Carlo).

    DP#9/DP#25 (issue #25): All Canada-specific fields live in
    jurisdiction_state['canada'], not as individual fields on SimState.
    Core simulation treats jurisdiction_state as opaque data (DP#8) — it passes
    it through without interpreting it.

    Universal fields (non-registered accounts, mortgage, generic HELOC debt)
    remain at the top level.
    """
    # Non-registered (universal)
    non_reg_balance: float = 0.0
    non_reg_acb: float = 0.0

    # Mortgage / generic HELOC debt (universal)
    mortgage_balance: float = 0.0
    heloc_balance: float = 0.0

    # Issue #688: the liquid emergency reserve -- the first, cheapest source
    # the forced-liquidation waterfall draws when required outflows exceed
    # available inflows in a year (simulation_rules.apply_solvency, #679).
    # Universal (not jurisdiction-specific): a cash reserve is not a tax
    # construct.
    #
    # This is a CASH SLEEVE CARVED OUT of the account named in
    # `assumptions.emergency_reserve.held_in` -- NOT extra money layered on
    # top of it. `SimState.initial` reduces the host account's balance by
    # exactly this amount, so the household's TOTAL assets are unchanged by
    # declaring a reserve; what changes is how much of them is invested at
    # the portfolio return versus parked at the reserve's own (much lower)
    # cash rate. That carve-out is the whole trade the household is making,
    # and modelling the reserve as free extra money would delete it.
    emergency_reserve_balance: float = 0.0

    # Issue #936: the balance parked in a taken deposit product (a HISA, a
    # term/GIC, a promotional teaser -- one generic mechanism). Universal (not
    # jurisdiction-specific): a cash deposit is not a tax construct. Like the
    # emergency reserve above, this is money CARVED OUT of the account named in
    # the product's `funding_source` -- NOT extra money layered on top.
    # `SimState.initial` reduces that host account by exactly this amount, so
    # declaring/taking a product never changes the household's TOTAL assets;
    # what changes is how much of them earns the deposit's interest rate versus
    # the funding source's own (e.g. market) rate -- that carve is the whole
    # take-vs-leave trade the optimizer prices (#936). 0.0 until a scenario
    # TAKES a product (config.deposit_product is not None); a household with no
    # product keeps this at 0.0 and the golden trajectory is byte-identical
    # (DP#32). Grown each year by simulation_rules.apply_deposit_product_growth
    # at the rate its rate_schedule prescribes, on the portion up to any
    # rate_eligible_cap, net of interest tax.
    deposit_product_balance: float = 0.0

    # Issue #689: the revolving credit facility's DRAWN balance -- a
    # facility DISTINCT from heloc_balance above (see SimulationConfig.
    # credit_facility_limit's docstring for why they are never merged).
    # Universal, same reasoning as heloc_balance: 0.0 until the #679
    # waterfall actually draws it in a shortfall year.
    credit_facility_balance: float = 0.0

    # Issue #763: closed-end consumer loans (car_loan/student_loan/
    # personal_loan) -- per-loan DRAWN balances, one entry per loan in
    # SimulationConfig.consumer_loans (parallel lists, matched by index).
    # Universal (not jurisdiction-specific): an amortizing consumer debt is
    # not a tax construct. The balance declines each year as the
    # simulation_rules.apply_consumer_loans rule amortizes it at the loan's
    # own declared rate / payment, reaching 0 at the payoff date -- a real
    # liability the household services, never silently dropped (DP#32).
    consumer_loan_balances: list = field(default_factory=list)

    # Issue #759: fixed-term, zero-interest installment obligations --
    # per-plan remaining-payment balances, one entry per plan in
    # SimulationConfig.installments (parallel lists, matched by index). The
    # balance is the sum of monthly payments + final balloon still owed
    # forward (a reporting figure, NOT folded into total_debt -- an
    # installment plan is a committed payment schedule, not a callable
    # borrowing). It declines each year as
    # simulation_rules.apply_installments applies the date-scheduled
    # payment, reaching 0 the year after the final payment date -- the plan
    # ENDS, it is not carried to the horizon (contrast annual_living_costs).
    installment_balances: list = field(default_factory=list)

    # Issue #692 (epic #690 bite 1): the couple's NON-principal properties'
    # net equity -- one float per property in SimulationConfig.properties
    # (parallel list, matched by index). Universal (not jurisdiction-specific):
    # a cottage/rental's equity is a balance-sheet asset, not a tax construct.
    # total_assets() sums this in, so the household's declared real estate is
    # on the annual balance sheet rather than silently truncated to the first
    # principal residence found (#692). Empty for a household with no such
    # property (the golden path) -> total_assets unchanged (DP#32). This bite
    # carries a STATIC figure (no appreciation/amortization); the property
    # dynamics are later bites (#693-#697), so the value is carried forward
    # unchanged year over year.
    property_equities: list = field(default_factory=list)

    # Issue #967: the OUTSTANDING balance of each mid-horizon mortgage
    # originated by a property's `purchase.financing` -- one float per
    # financed property, parallel to `config.properties` by index (a
    # property with no financing carries 0.0; the list always matches
    # config.properties in length, so the index alignment
    # `consumer_loan_balances`/`installment_balances` already use holds).
    # Universal (not jurisdiction-specific): an amortizing mortgage is a
    # balance-sheet liability, not a tax construct. The balance ORIGINATES
    # at the property's purchase year (seeded 0.0 at SimState.initial --
    # the mortgage does not exist before its origination year) and declines
    # each year as the `second_property_mortgage` rule amortizes it from the
    # precomputed schedule, reaching 0 at the payoff year. Folded into
    # `total_debt` so the balance sheet sees the real household debt. A
    # household with no financed property carries an all-zero list ->
    # total_debt unchanged (DP#32). The principal is the couple's share
    # (mapped at the couple's ownership %), mirroring `consumer_loan_balances`.
    second_property_mortgage_balances: list = field(default_factory=list)

    # DP#9: Jurisdiction-specific state (opaque to core simulation engine)
    jurisdiction_state: Dict[str, object] = field(default_factory=dict)

    def __deepcopy__(self, memo):
        """Deep-copy jurisdiction_state when copying SimState.

        DP#26 requires SimState to support forking for optimizer modes.
        Without this override, copy.deepcopy() would shallow-copy
        jurisdiction_state, so mutating FHSA fields on one fork corrupts
        the other. This override ensures each copy gets its own deep copy.
        """
        new_state = replace(self)
        new_state.jurisdiction_state = deepcopy(self.jurisdiction_state, memo)
        new_state.consumer_loan_balances = list(self.consumer_loan_balances)
        new_state.installment_balances = list(self.installment_balances)
        new_state.property_equities = list(self.property_equities)
        new_state.second_property_mortgage_balances = list(
            self.second_property_mortgage_balances)
        return new_state

    @classmethod
    def fork(cls, state: 'SimState', **changes) -> 'SimState':
        """Create an independent fork of state with deep-copied jurisdiction_state.

        DP#26: SimState must support forking for optimizer modes. This method
        ensures jurisdiction_state is deep-copied so mutations on the fork
        don't affect the original. Use this instead of dataclasses.replace()
        when you need independent state branches.
        """
        changes.setdefault('jurisdiction_state', deepcopy(state.jurisdiction_state))
        changes.setdefault('consumer_loan_balances', list(state.consumer_loan_balances))
        changes.setdefault('installment_balances', list(state.installment_balances))
        changes.setdefault('property_equities', list(state.property_equities))
        changes.setdefault('second_property_mortgage_balances',
                           list(state.second_property_mortgage_balances))
        return replace(state, **changes)

    def __post_init__(self):
        """Ensure jurisdiction_state['canada'] dict exists with all required keys (DP#9, DP#25).

        Core simulation stores jurisdiction data as plain dicts, not
        Canada-specific classes. The Canada adapter (countries/canada/adapter.py)
        populates this with the required keys when creating initial state.
        """
        if 'canada' not in self.jurisdiction_state:
            self.jurisdiction_state = dict(self.jurisdiction_state)
            self.jurisdiction_state['canada'] = _default_canada_state()

    @classmethod
    def initial(cls, config: SimulationConfig) -> 'SimState':
        """Create initial SimState from a SimulationConfig.

        Reads family member data for account rooms and balances.
        All Canada-specific fields are populated in jurisdiction_state['canada'].

        Issue #577 / DP#18 / DP#32: ``margin_available`` is undrawn HELOC
        *room* (a credit limit), not a balance owed. It used to be recorded
        as ``heloc_balance`` debt unconditionally, so a household that never
        drew its margin still had that debt compound, unserviced, for the
        entire projection — a dollar of debt with no borrowed dollar and no
        invested dollar behind it (DP#18's money-conservation invariant).
        Zero is the correct value here, not a fallback masking a missing
        input (DP#32): nothing has actually been borrowed at construction
        time. An actual draw is either (a) a Smith-Manoeuvre readvance,
        tracked separately as mortgage principal is paid down (see
        jurisdiction_state['canada']['readvance_heloc_balance']), or (b) an
        explicit lump-sum draw the caller requests of *this* simulation run
        (FamilySimulation(lump_sum=...), used by simulate.py/optimize.py's
        cash-out and margin-draw strategies) — which FamilySimulation books
        onto heloc_balance itself once the money is actually invested,
        because SimState has no way to know about a draw that hasn't been
        decided yet.
        """
        primary = config.member_by_role('primary', {})  # #699 seam
        spouse = config.member_by_role('spouse', {})

        # Opening registered balances. RRSP/TFSA are legally per-person, so the
        # canonical home is the family member (DP#4). A household total declared
        # under portfolio.accounts.{rrsp,tfsa}.balance is *allocated to the
        # primary* rather than silently dropped — input that is present must
        # reach the simulation (DP#16). Without this a household with $700k of
        # registered savings was projected as if it had none.
        _pf_accounts = (config.portfolio_data or {}).get('accounts', {})

        # Opening non-registered balance/ACB (#599 follow-up, epic #603 Phase
        # 2b). Unlike rrsp/tfsa above, there is no member-level
        # `non_reg_balance` alias to prefer — portfolio.accounts.non_reg.
        # {balance,cost_basis} is the ONLY place a non-reg opening balance can
        # be declared at all (test_schema_coverage.py's DEAD_ALLOWLIST used to
        # carry this pair as parsed-but-never-read-back; this is that wiring).
        # A household that never states a non-reg opening balance still
        # starts at $0 — that omission is absence, not a default coercing a
        # supplied value (DP#32): `.get('balance', 0)` only fires when the
        # key is genuinely missing, and PortfolioConfig.from_dict already
        # guarantees `_pf_accounts['non_reg']` is absent entirely rather than
        # `{}` when the config declares nothing under portfolio.accounts.
        _non_reg_cfg_raw = _pf_accounts.get('non_reg')
        _non_reg_cfg = {} if _non_reg_cfg_raw is None else _non_reg_cfg_raw
        _non_reg_balance_raw = _non_reg_cfg.get('balance', 0)
        non_reg_opening_balance = 0 if _non_reg_balance_raw is None else _non_reg_balance_raw
        # cost_basis absent/None ("unknown", per the input contract) is NOT
        # coerced to 0 -- that would fabricate a $0 ACB (100% unrealized
        # gain) out of thin air. The honest DP#13 fallback for a genuinely
        # unknown cost basis is "assume no unrealized gain yet" (ACB ==
        # balance), not "assume the whole balance is gain."
        _non_reg_cost_basis = _non_reg_cfg.get('cost_basis')
        non_reg_opening_acb = (
            non_reg_opening_balance if _non_reg_cost_basis is None else _non_reg_cost_basis
        )

        def _opening(key: str, pf_key: str) -> tuple:
            """DP#32 (#606): whether to use the per-member balance or fall
            back to the household portfolio total must be decided by
            whether the member dict *supplied the key at all* -- not by
            whether the supplied value is truthy. An explicit
            ``rrsp_balance: 0`` / ``tfsa_balance: 0`` is a real, deliberate
            fact ("this person has no registered savings"); testing it with
            `if p_val or s_val` used to treat that 0 exactly like "the key
            was never supplied" and silently substitute the portfolio
            total meant for a *different* scenario (#574's dropped-balance
            bug's twin shape).
            """
            p_present = key in primary
            s_present = key in spouse
            if p_present or s_present:
                p_val = primary.get(key)
                s_val = spouse.get(key)
                return (0 if p_val is None else p_val), (0 if s_val is None else s_val)
            return (_pf_accounts.get(pf_key, {}).get('balance', 0) or 0), 0

        rrsp_p_open, rrsp_s_open = _opening('rrsp_balance', 'rrsp')
        tfsa_p_open, tfsa_s_open = _opening('tfsa_balance', 'tfsa')

        # issue #293: CRI/LIRA block is top-level in input.json
        # (config.lira_data). DP#9/#606: the member-embedded 'lira' dict was
        # a duplicate declaration of the same fact (#595) and, per the
        # comment this replaced, "never exists for real inputs" -- it was
        # dead weight that also had a DP#32 shape (an explicitly cleared
        # top-level `lira: {}`, meaning "no LIRA for this household," would
        # have fallen through to a stale member-embedded dict via `or`).
        # `config.lira_data` is a dataclass field with `default_factory=dict`
        # (simulation_config.py), so it is never actually absent on a real
        # SimulationConfig -- there is nothing left for a fallback to reach.
        lira_cfg = config.lira_data if config.lira_data is not None else {}

        n_children = len(config.children)
        resp_balances = [config.resp_current_balance / max(1, n_children)] * n_children

        # DP#19/issue #578: seed the composition buckets (contributions,
        # CESG, QESI -- earnings are the remainder) so the opening RESP
        # balance can be wound down by what it actually is, not a blended
        # number. Real composition data (accounts.resp_composition) always
        # wins; falls back to the resp_rules default split only when absent.
        from countries.canada.resp_rules import default_resp_composition
        _resp_comp = getattr(config, 'resp_composition', None) or {}
        if _resp_comp:
            _resp_contrib_total = _resp_comp.get('total_contributions', 0)
            _resp_cesg_total = _resp_comp.get('total_cesg_received', 0)
            _resp_qesi_total = _resp_comp.get('total_qesi_received', 0)
            # DP#32: a composition that is present but sums to nothing, against
            # a real balance, is incoherent input -- not a legitimate zero. Left
            # to fall through it would silently classify the ENTIRE balance as
            # investment earnings, over-taxing every EAP and (on the collapse
            # path) charging AIP penalty tax on contributions that were never
            # taxable. Fail loudly rather than answer confidently and wrongly.
            _declared = (_resp_contrib_total + _resp_cesg_total + _resp_qesi_total
                         + _resp_comp.get('investment_earnings', 0))
            if _declared <= 0 < config.resp_current_balance:
                raise ValueError(
                    "accounts.resp_composition was supplied but every bucket is zero, "
                    f"while accounts.resp_current_balance is {config.resp_current_balance:,.2f}. "
                    "A balance cannot be composed of nothing. Either omit resp_composition "
                    "entirely (the engine will then apply resp_rules.default_resp_composition), "
                    "or state the real contributions/CESG/QESI/earnings breakdown."
                )
        elif config.resp_current_balance > 0:
            _default_comp = default_resp_composition(config.resp_current_balance)
            _resp_contrib_total = _default_comp['total_contributions']
            _resp_cesg_total = _default_comp['total_cesg_received']
            _resp_qesi_total = _default_comp['total_qesi_received']
        else:
            _resp_contrib_total = _resp_cesg_total = _resp_qesi_total = 0.0
        resp_contributions = [_resp_contrib_total / max(1, n_children)] * n_children
        resp_cesg = [_resp_cesg_total / max(1, n_children)] * n_children
        resp_qesi = [_resp_qesi_total / max(1, n_children)] * n_children

        # Issue #577: margin_available is undrawn room, not a balance owed.
        # See the docstring above — booking it here unconditionally is the
        # bug. Debt is only booked once a draw is actually decided (see
        # FamilySimulation.__init__'s lump_sum handling).
        # Issue #1039: EXCEPT the declared OPENING DRAWN position --
        # liabilities[kind=heloc].balance.amount honoured as the true starting
        # heloc_balance, so a household already mid-strategy starts from its
        # real position. input_contract.py maps the key only when the contract
        # declares a drawn balance WITH its deductibility, so 0.0 here is the
        # documented undrawn state (#577), never a coerced zero (DP#32).
        initial_heloc = config.heloc_opening_balance

        # FHSA room calculation. DP#9/#606: `fhsa_room` (bare) was a
        # legacy member-input alias for the canonical `fhsa_room_accumulated`
        # key (used everywhere else -- scenario_discovery.py,
        # cashout_optimizer.py, module_registry.py's trigger field) -- a
        # duplicate declaration of the same fact (#595). It also had a
        # DP#32 shape: a legitimate `fhsa_room_accumulated: 0` ("this
        # person's FHSA room is fully used") fell through to the alias via
        # `or`. Deleted rather than hardened, per DP#9.
        #
        # #647: FHSA is a single HOUSEHOLD pot in this engine (#643 -- not
        # yet split per owner), but the account it represents legally
        # belongs to ONE named person, who may be the primary OR the
        # spouse. Reading only `primary.get(...)` silently dropped every
        # FHSA opened by the spouse (input_contract.py's mapper attributes
        # fhsa_room_accumulated/fhsa_balance to whichever of primary/spouse
        # actually owns the account, and refuses a document where BOTH do
        # -- see _map_registered_balances -- so at most one of the two
        # dicts below ever genuinely carries either key).
        def _fhsa_household(key: str):
            """Explicit presence, never `or`: a real 0 must not be confused
            with absence (DP#32)."""
            if key in primary:
                v = primary.get(key)
                return 0 if v is None else v
            if key in spouse:
                v = spouse.get(key)
                return 0 if v is None else v
            return 0

        fhsa_room_val = _fhsa_household('fhsa_room_accumulated')
        fhsa_balance_val = _fhsa_household('fhsa_balance')
        # CRA s.146.6(1): participation room = annual limit + carry-forward,
        # capped at annual_limit + carry_forward_max (Canada FHSA module owns the figures).
        _fhsa_annual, _fhsa_carry_max, _fhsa_lifetime = _canada_fhsa_limits()
        fhsa_room_val = min(fhsa_room_val, _fhsa_annual + _fhsa_carry_max)

        # DP#9/DP#25/issue #25: All Canada fields in jurisdiction_state['canada'] dict.
        # Issue #700/#643: seed the per-adult RRSP store in canonical adult
        # order (primary first). Each adult carries their own RRSP + own room;
        # the spouse also carries any spousal RRSP they are the annuitant of.
        # #647: the spousal-RRSP pot is structurally tied to the SPOUSE's RRIF
        # minimum (simulation_rules.py pairs opening_spouse_rrsp_balance with
        # opening_spousal_rrsp_balance) -- input_contract.py's mapper only ever
        # attributes a spousal_rrsp account's balance to the spouse member dict
        # (a primary-owned spousal_rrsp is refused there, since this engine has
        # no pot for that -- #643), so the annuitant is the spouse. Spousal RRSP
        # shares the contributor's room, so it carries no own room.
        # #699: key by the stable entity id; a config constructed directly
        # (not via from_dict, which sets the schema person_id) falls back to
        # the role label -- a stable identity in the two-adult world. .get with
        # an explicit default is the DP#32-correct absence idiom, not `or`.
        adult_rrsp: dict = {}
        if primary:
            adult_rrsp[primary.get('id', primary.get('role'))] = {
                'own': rrsp_p_open,
                'own_room': primary.get('rrsp_room_accumulated', 0),
                'spousal_as_annuitant': 0.0,
            }
        if spouse:
            adult_rrsp[spouse.get('id', spouse.get('role'))] = {
                'own': rrsp_s_open,
                'own_room': spouse.get('rrsp_room_accumulated', 0),
                'spousal_as_annuitant': spouse.get('spousal_rrsp_balance', 0),
            }

        # Issue #700/#643: seed the per-adult TFSA store in canonical adult
        # order (primary first), keyed by the same stable entity id as the RRSP
        # store (role-label fallback for a directly-constructed config).
        adult_tfsa: dict = {}
        if primary:
            adult_tfsa[primary.get('id', primary.get('role'))] = {
                'balance': tfsa_p_open,
                'room': primary.get('tfsa_room_accumulated', 0),
            }
        if spouse:
            adult_tfsa[spouse.get('id', spouse.get('role'))] = {
                'balance': tfsa_s_open,
                'room': spouse.get('tfsa_room_accumulated', 0),
            }

        # Issue #899 (part a): seed each ADDITIONAL accumulating adult (beyond
        # the primary couple) into the per-adult RRSP/TFSA stores as slots >= 2.
        # These adults are admitted by input_contract only when they are pure
        # accumulators across the horizon (retired/benefit-drawing extras are
        # refused to #901), so only their OWN RRSP/TFSA opening balances + room
        # are seeded here -- no spousal-annuitant / FHSA / LIRA / LIF slot, which
        # the accumulation-only compute (step_extra_adult_accounts) does not
        # drive. For a two-adult household config.adults()[2:] is empty, so this
        # loop never runs and the stores are byte-identical to the pre-#899 seed.
        for extra in config.adults()[2:]:
            _xid = extra.get('id', extra.get('role'))
            adult_rrsp[_xid] = {
                'own': extra.get('rrsp_balance', 0),
                'own_room': extra.get('rrsp_room_accumulated', 0),
                'spousal_as_annuitant': 0.0,
            }
            adult_tfsa[_xid] = {
                'balance': extra.get('tfsa_balance', 0),
                'room': extra.get('tfsa_room_accumulated', 0),
            }

        # Issue #700/#643/#704: seed the per-adult FHSA / LIRA / LIF stores.
        #
        # FHSA slot 0 drives the still-single-slot compute and is byte-identical
        # to the old singleton: _fhsa_household routed the household FHSA
        # (whichever adult owns it) into one pot, and it lands here keyed by the
        # primary id. A SECOND adult's FHSA is now representable (#704 --
        # input_contract's dual-owner refusal relaxed); when BOTH adults own one
        # the spouse's gets slot 1 and compounds independently (rebuild's growth
        # pass), while attribution of the sole-spouse-FHSA case to its true owner
        # and per-adult FHSA contributions are DEFERRED (Step 4 follow-up).
        def _member_fhsa(m: dict, key: str):
            """Explicit presence, never `or` (DP#32): a real 0 is not absence."""
            if key in m:
                v = m.get(key)
                return 0 if v is None else v
            return 0

        adult_fhsa: dict = {}
        if primary:
            adult_fhsa[primary.get('id', primary.get('role'))] = {
                'balance': fhsa_balance_val,
                'room': fhsa_room_val,
                'lifetime_used': 0.0,
                'lifetime_limit': v if (v := primary.get('fhsa_lifetime_limit')) is not None else _fhsa_lifetime,
            }
        primary_owns_fhsa = ('fhsa_balance' in primary) or ('fhsa_room_accumulated' in primary)
        spouse_owns_fhsa = spouse and (('fhsa_balance' in spouse) or ('fhsa_room_accumulated' in spouse))
        if spouse_owns_fhsa and primary_owns_fhsa:
            adult_fhsa[spouse.get('id', spouse.get('role'))] = {
                'balance': _member_fhsa(spouse, 'fhsa_balance'),
                'room': min(_member_fhsa(spouse, 'fhsa_room_accumulated'), _fhsa_annual + _fhsa_carry_max),
                'lifetime_used': 0.0,
                'lifetime_limit': v if (v := spouse.get('fhsa_lifetime_limit')) is not None else _fhsa_lifetime,
            }

        # LIRA/LIF: one household account today (config.lira_data), so a single
        # primary-keyed slot -- the birth_year fallback is the primary's, so the
        # account is conceptually the primary's. A second adult's LIRA/LIF and
        # its 2-owner conversion mechanics are DEFERRED (Step 4 follow-up).
        adult_lira: dict = {}
        adult_lif: dict = {}
        if primary:
            _pid = primary.get('id', primary.get('role'))
            adult_lira[_pid] = {
                'balance': lira_cfg.get('balance', 0),
                # DP#32/#621: an explicit `lira.birth_year: 0` is bad data but
                # must surface as such rather than silently inheriting the
                # primary's -- explicit absence-testing, not truthiness.
                'birth_year': (v if (v := lira_cfg.get('birth_year')) is not None
                               else primary.get('birth_year', 0)),
                'jurisdiction': lira_cfg.get('jurisdiction', 'federal'),
                'reference_rate': lira_cfg.get('reference_rate', 0.06),
                # Issue #708: elected early-conversion year (0 = no election;
                # the age-71 backstop then applies -- unchanged behaviour).
                'conversion_year': (v if (v := lira_cfg.get('conversion_year')) is not None
                                    else 0),
            }
            adult_lif[_pid] = {
                'balance': 0.0,  # LIF created from CRI/LIRA conversion at age 71
                'birth_year': 0,  # Set at conversion time
                'jurisdiction': lira_cfg.get('jurisdiction', 'federal'),
                'reference_rate': lira_cfg.get('reference_rate', 0.06),
            }

        canada_state = _default_canada_state()
        canada_state.update({
            'adult_rrsp': adult_rrsp,
            'adult_tfsa': adult_tfsa,
            'resp_balances': resp_balances,
            'resp_contributions': resp_contributions,
            'resp_cesg': resp_cesg,
            'resp_qesi': resp_qesi,
            # Issue #700/#643/#704: FHSA / LIRA / LIF are now per-adult stores
            # (built above), replacing the singleton fhsa_*/lira_*/lif_* pots.
            # #647: fhsa_balance was previously absent from this dict and fell
            # through to _default_canada_state()'s 0.0; the per-adult seed now
            # carries the declared opening balance in slot 0.
            # DP#16/issue #293: CRI/LIRA is auto-included when config.lira_data
            # is present (top-level of input.json, separate from the member RRSP).
            'adult_fhsa': adult_fhsa,
            'adult_lira': adult_lira,
            'adult_lif': adult_lif,
        })

        # Epic #841 bite 2 / issue #812: seed each child's OWN opening
        # registered accounts + room, parallel to config.children. The fold
        # then grows them from the child's OWN income year over year (see
        # simulate_year_pure). Empty list for a household with no children --
        # inert for the golden household (which declares children as RESP
        # beneficiaries only, with no income, room, or owned accounts).
        canada_state['child_accounts'] = _initial_child_accounts(config.children)

        # ── Emergency reserve (issue #688): a CASH SLEEVE CARVED OUT of the
        # account it is declared to be held in -- never extra money on top.
        #
        # Money conservation (DP#18): the sleeve is subtracted from its host
        # account's balance, so `total_assets()` is unchanged by declaring a
        # reserve. What changes is the SPLIT: `reserve` sits at the reserve's
        # own cash rate, and the host account's remainder stays invested at
        # the portfolio return. That split IS the trade the household is
        # making, and modelling the reserve as free extra money would delete
        # the very cost the sweep exists to price. (An earlier draft of this
        # PR added it on top; `tests/test_input_contract.py`'s
        # dollar-for-dollar balance-conservation guard caught it -- the guard
        # working exactly as designed.)
        #
        # `target_months is None` = no reserve block declared at all = a $0
        # reserve, which the ruin report STATES rather than assumes away
        # (DP#32). It is not a fallback: nothing is being substituted for a
        # value the household supplied.
        reserve_opening = reserve_target(
            config.emergency_reserve_target_months,
            annual_living_costs=(config.living_costs or 0.0),
            # issue #763: the reserve/runway target (#758) is sized against
            # BOTH halves of the household's essential debt service -- the
            # mortgage (above) AND the closed-end consumer loans. A car
            # payment the household genuinely must make is exactly the
            # outflow an emergency reserve exists to bridge; omitting it
            # understates the target the way omitting the mortgage would.
            annual_debt_service=(_annual_debt_service(config)
                                 + _annual_consumer_loan_service(config)
                                 # issue #759: the installment payment is a
                                 # must-pay outflow in the same
                                 # non-compressible debt-service channel as
                                 # the mortgage + consumer loans -- a payment
                                 # plan the household is legally bound to is
                                 # exactly the outflow a reserve exists to
                                 # bridge. Omitting it understates the target
                                 # the way omitting the car payment would.
                                 + _annual_installment_service(config)),
        )
        held_in = config.emergency_reserve_held_in

        non_reg_after_carve = non_reg_opening_balance
        non_reg_acb_after_carve = non_reg_opening_acb
        if reserve_opening > 0 and held_in is not None:
            # A held_in the engine cannot resolve to a real account AT ALL
            # (a typo, an unsupported kind) still fails loudly, inside
            # _host_account_balance -- that is an unanswerable question.
            available = _host_account_balance(held_in, canada_state, non_reg_opening_balance)
            # A household cannot hold more cash in an account than that
            # account contains. Falling short of the declared target is a
            # real, reportable fact ("you are N months short"), not an error.
            #
            # An EMPTY host account is not a different kind of thing -- it is
            # the extreme of the same fact: the household holds a $0 reserve
            # and is short by its entire declared target. An earlier draft
            # raised here, which was self-contradictory (a $1 balance clamped
            # and reported; a $0 balance crashed) and actively harmful: inside
            # the optimizer, issue #657's `except Exception: score = -inf`
            # would have swallowed the crash and silently ranked the strategy
            # last, indistinguishable from a merely bad one. Zero is a value,
            # not an error (DP#32) -- it is reported, by
            # emergency_reserve_months_covered and by next_action's
            # "you are N months short of the reserve you declared".
            reserve_opening = min(reserve_opening, available)
            if held_in == 'non_reg':
                non_reg_after_carve = non_reg_opening_balance - reserve_opening
                # DP#19: the carve-out is a reallocation WITHIN the account,
                # not a disposition -- the ACB travels with the dollars, so
                # the remaining invested slice keeps a proportionate basis.
                if non_reg_opening_balance > 0:
                    non_reg_acb_after_carve = non_reg_opening_acb * (
                        non_reg_after_carve / non_reg_opening_balance)
            else:
                _carve_from_canada(held_in, canada_state, reserve_opening)

        # Issue #936: carve the TAKEN deposit product's fund_amount out of its
        # funding_source (money-conserving, capability #5). Absent product
        # (config.deposit_product is None -- the "leave it" baseline, and every
        # no-product household) -> 0.0, so total_assets and the golden trajectory
        # are byte-identical (DP#32). A household cannot move more cash into the
        # deposit than the source holds, so the funded amount is clamped to the
        # available balance -- a partial take is a real outcome, reported, not
        # an error (the same philosophy the reserve carve above applies to a
        # short host account). The ACB travels with the dollars (DP#19), same
        # as the reserve's non_reg carve: a reallocation within the
        # non-registered account, not a disposition.
        deposit_product_opening = 0.0
        product = config.deposit_product
        if product is not None:
            funding_source = product['funding_source']
            if funding_source != 'non_reg':
                raise ValueError(
                    f"deposit_product {product.get('id', '?')!r} declares "
                    f"funding_source={funding_source!r}, but only 'non_reg' is "
                    f"supported: a deposit's 'new net deposits' come from the "
                    f"household's existing non-registered cash (#936). Funding it "
                    f"from a registered/other source is a different, unmodelled "
                    f"question -- refused rather than silently funded from the "
                    f"wrong account (DP#32)."
                )
            deposit_product_opening = min(product['fund_amount'], max(0.0, non_reg_after_carve))
            if non_reg_after_carve > 0:
                non_reg_acb_after_carve = non_reg_acb_after_carve * (
                    (non_reg_after_carve - deposit_product_opening) / non_reg_after_carve)
            non_reg_after_carve = non_reg_after_carve - deposit_product_opening

        # Issue #1039: seed the opening margin trace from the DECLARED
        # deductibility of the opening drawn balance -- the original
        # borrowing's purpose is a historical fact carried in by the
        # snapshot, not a simulation decision, so the trace is derived from
        # the declared ratio (investment_portion = p) and carried forward by
        # the 'borrowing_purpose' rule's no-lump-sum path exactly like a
        # year-0 trace would be. Inert (untouched, all-zero) for a household
        # with no opening draw -- the golden path, byte-identical (DP#32).
        if config.heloc_opening_balance > 0:
            _opening_portion = config.heloc_opening_investment_portion
            canada_state['margin_tracing'] = {
                'total_advances': config.heloc_opening_balance,
                'investment_advances': config.heloc_opening_balance * _opening_portion,
                'rrsp_advances': 0.0,
                'tfsa_advances': 0.0,
                'personal_draws': config.heloc_opening_balance * (1.0 - _opening_portion),
            }

        return cls(
            mortgage_balance=config.mortgage_balance,
            heloc_balance=initial_heloc,
            non_reg_balance=non_reg_after_carve,
            non_reg_acb=non_reg_acb_after_carve,
            emergency_reserve_balance=reserve_opening,
            deposit_product_balance=deposit_product_opening,
            # issue #763: seed each consumer loan's opening balance from the
            # contract's declared balance (a fact), parallel to
            # config.consumer_loans by index. The fold amortizes it from here.
            consumer_loan_balances=[loan['balance'] for loan in config.consumer_loans],
            # issue #759: seed each installment plan's opening remaining-payment
            # balance = the full forward obligation (N monthly payments + the
            # optional final balloon), parallel to config.installments by index.
            # The fold drains it via apply_installments from here; it reaches 0
            # the year after the final payment date. NOT a callable debt --
            # excluded from total_debt (an installment plan is a committed
            # payment schedule, not a borrowing against the estate).
            installment_balances=[
                plan['monthly_amount'] * plan['number_of_payments']
                + plan['final_payment']
                for plan in config.installments],
            # issue #692: seed each non-principal property's net equity (value -
            # secured mortgage, at the couple's ownership share), parallel to
            # config.properties by index, summed into total_assets so the
            # household's declared real estate is on the annual balance sheet.
            # issue #696: a property with a dated mid-horizon purchase is not yet
            # owned at the start year, so its opening equity is ZERO until its
            # purchase year (_property_equity_for_year); a property held from
            # year 0 seeds its full net_equity, byte-identical to #692.
            property_equities=[
                _property_equity_for_year(prop, config.start_year, config.start_year)
                for prop in config.properties],
            # Issue #967: seed each financed property's mid-horizon mortgage
            # balance at 0.0 -- the mortgage ORIGINATES at its purchase year,
            # not at year 0 (it does not exist before its origination year),
            # so the year-0 balance is zero for every financed property. The
            # `second_property_mortgage` rule originates the balance from the
            # precomputed schedule in the purchase year. A property with no
            # financing also carries 0.0 here, so the list matches
            # config.properties in length (parallel-by-index). An all-zero list
            # for a household with no financed property -> total_debt
            # unchanged (DP#32).
            second_property_mortgage_balances=[
                0.0 for _prop in config.properties],
            jurisdiction_state={'canada': canada_state},
        )

    def total_assets(self) -> float:
        """Sum of all account balances.

        Per issue #230: CRI/LIRA and LIF balances are included in total assets.
        CRI/LIRA is a locked-in retirement account that holds pension funds —
        it's a real asset even though it can't be withdrawn before conversion.
        LIF is the decumulation vehicle after age 71 conversion.

        Issue #688: the emergency reserve is counted here exactly ONCE, and
        adding it does not inflate the balance sheet -- it is a cash sleeve
        that ``initial()`` already SUBTRACTED from its host account, so
        (host_remainder + reserve) == the account's declared balance.
        Declaring a reserve therefore changes how the household's money is
        SPLIT (invested vs. parked in cash), never how much of it there is.
        """
        canada = self.jurisdiction_state.get('canada', {})
        return (
            adult_rrsp_total(canada) +  # #700/#643: per-adult RRSP store
            adult_tfsa_total(canada) +  # #700/#643: per-adult TFSA store
            self.non_reg_balance +
            sum(canada.get('resp_balances', [])) +
            adult_fhsa_total(canada) +  # #700/#643/#704: per-adult FHSA store
            canada.get('sm_investment_balance', 0) +
            adult_lira_total(canada) +  # #700/#643: per-adult LIRA store
            adult_lif_total(canada) +   # #700/#643: per-adult LIF store
            self.emergency_reserve_balance +
            # Issue #936: the deposit-product balance, counted here exactly ONCE.
            # It does not inflate the balance sheet -- initial() SUBTRACTED it
            # from its funding_source, so (source_remainder + deposit) == the
            # source's declared balance. 0.0 for a household with no taken
            # product (the golden path) -> no change (DP#32).
            self.deposit_product_balance +
            # Issue #692: the couple's non-principal real estate's net equity.
            # Empty for a household with only a principal residence (the golden
            # path) -> no change (DP#32).
            sum(self.property_equities)
        )

    def total_debt(self) -> float:
        """Sum of all debts."""
        canada = self.jurisdiction_state.get('canada', {})
        return (
            self.mortgage_balance +
            self.heloc_balance +
            canada.get('readvance_heloc_balance', 0) +
            self.credit_facility_balance +
            # issue #763: closed-end consumer loans are real household debt --
            # unsecured, but still a liability against the estate. Folded in
            # so net_assets and the balance sheet see the whole picture.
            sum(self.consumer_loan_balances)
        )

    def net_assets(self) -> float:
        return self.total_assets() - self.total_debt()


def margin_draw_for_lump_sum(lump_sum: float, margin_available: float) -> float:
    """The portion of a year-0 lump sum that is a HELOC margin *draw* (#577).

    DP#3: a pure function, so both engines that invest a year-0 lump sum --
    ``FamilySimulation`` (simulate.py's path) and ``Optimizer._run_simulation``
    (the Grid/Scipy optimizers' path) -- book the resulting debt from ONE rule
    rather than each re-deriving it. Getting this wrong in either direction is
    a money-conservation break (DP#18):

      - Book the whole ``margin_available`` unconditionally and a household
        that never drew a dollar carries phantom debt that compounds
        unserviced forever (the #577 bug, when SimState.initial did this).
      - Book nothing and an engine that DOES draw the margin invests borrowed
        money it never records owing -- money from nowhere, which would make
        every margin-drawing strategy look free to the optimizer's ranking.

    The draw is capped at ``margin_available`` rather than being the whole
    ``lump_sum``, because callers size the lump sum as
    ``margin_available + cash_out`` (simulate.py, optimize.py,
    scipy_optimizer.py): the cash-out half is a *mortgage* increase, already
    booked once as mortgage debt (#257). Counting it here too would record the
    same borrowed dollar as both HELOC and mortgage debt.

    Args:
        lump_sum: Year-0 lump sum the caller is investing (>= 0).
        margin_available: The HELOC credit *limit* -- undrawn room.

    Returns:
        The drawn HELOC balance to book, in [0, margin_available].
    """
    if lump_sum <= 0 or margin_available <= 0:
        return 0.0
    return min(lump_sum, margin_available)


def initial_state_for_run(config: SimulationConfig, lump_sum: float = 0.0) -> 'SimState':
    """Build the opening ``SimState`` for a run, including any year-0 margin draw.

    Issue #583/DP#26: every engine entry point that starts a fold from
    scratch -- ``FamilySimulation.__init__`` (simulate.py's path),
    ``Optimizer._run_simulation`` (the Grid/Scipy/Monte-Carlo optimizers'
    path), and ``DPOptimizer`` (the dynamic-programming path) -- used to call
    ``SimState.initial(config)`` directly and then (in two of the three)
    re-derive the ``margin_draw_for_lump_sum`` booking by hand. Three call
    sites computing "the opening state" is exactly the shape that produced
    #577: one of them booked the draw, one didn't, and nothing enforced that
    they agreed. Routing all three through this single constructor makes
    that class of divergence structurally impossible -- there is only one
    place "the opening state for a lump sum" is computed, so there is
    nothing left to drift.

    A caller that never draws a lump sum (``lump_sum=0.0``, the default) gets
    exactly ``SimState.initial(config)`` back -- ``margin_draw_for_lump_sum``
    is a no-op at zero, so this is a pure consolidation, not a behaviour
    change, for every existing caller that didn't already book a draw.

    Args:
        config: Simulation configuration.
        lump_sum: Year-0 lump sum this run is investing (margin draw +
            cash-out), if any. Defaults to 0.0 (no draw).

    Returns:
        The opening SimState, with heloc_balance set to the drawn portion
        of lump_sum (capped at margin_available; see
        margin_draw_for_lump_sum) PLUS any declared opening drawn position
        (issue #1039: config.heloc_opening_balance, a fact the contract
        carries -- margin_available has already been reduced by that draw
        upstream, so the cap applies to the room that genuinely remains).
    """
    state = SimState.initial(config)
    state.heloc_balance = (
        config.heloc_opening_balance
        + margin_draw_for_lump_sum(lump_sum, config.margin_available))
    return state


def compute_heloc_deductible_proportion(
    tracing: dict, yield_rate: float | None = None
) -> float:
    """Compute the deductible proportion of HELOC interest from tracing state.

    Federal deductibility under CRA §20(1)(c) requires BOTH:
      1. Purpose: the funds were traced to investment-purpose advances (not
         RRSP/TFSA, whose income is sheltered). This is the proportion below.
      2. Reasonable expectation of income: the investment must be capable of
         producing income (yield > 0). A pure-growth, zero-yield holding has
         no reasonable expectation of income, so the interest does NOT qualify
         federally — regardless of how cleanly the advance is traced.

    Args:
        tracing: Dict with 'total_advances' and 'investment_advances' keys.
        yield_rate: Expected distribution/income yield on the SM investment.
            When provided and <= 0, the §20(1)(c) income-producing test fails
            and the deductible proportion is 0.0. When None, only the
            purpose-tracing test is applied (legacy behaviour for callers that
            don't model yield).

    Returns:
        Deductible proportion in [0, 1].
    """
    if yield_rate is not None and yield_rate <= 0:
        # No reasonable expectation of income: fails CRA §20(1)(c) federally.
        return 0.0
    total = tracing.get('total_advances', 0)
    if total <= 0:
        return 0.0
    return tracing.get('investment_advances', 0) / total


def ledger_undeducted_total(ledger: list) -> float:
    """Compute the total undeducted amount in an RRSP ledger.

    Compute the total undeducted amount in an RRSP ledger.
    The ledger is a list of dicts with 'deducted' and 'amount' keys.
    """
    return sum(e['amount'] for e in ledger if not e['deducted'])


def ledger_total_claimed(ledger: list, year: int = None) -> float:
    """Compute the total claimed deductions in an RRSP ledger.

    Args:
        ledger: List of contribution dicts
        year: If provided, only count deductions claimed in this year
    """
    total = 0.0
    for e in ledger:
        if e['deducted']:
            if year is None or e.get('deduction_year') == year:
                total += e['amount']
    return total


def ledger_total_tax_savings(ledger: list) -> float:
    """Compute the total tax savings from all deducted contributions in an RRSP ledger."""
    return sum(
        e['amount'] * (e.get('deduction_marginal_rate') or 0)
        for e in ledger if e['deducted']
    )


def _new_heloc_tracing(old_tracing: dict, **overrides) -> dict:
    """Create a new HELOC tracing dict from an old one with overrides."""
    new = dict(old_tracing)
    new.update(overrides)
    return new


def borrowing_purpose_tracings(
    lump_sum: float,
    lump_non_reg: float,
    margin_available: float,
    mortgage_balance: float,
    opening_margin_tracing: dict | None = None,
) -> tuple:
    """Trace the year-0 leveraged lump sum's TWO borrowings to purpose --
    ITA s.20(1)(c) -- returning ``(advance_tracing, margin_tracing)`` (#850).

    ## Why this exists

    #849 states the household's actual question as a DEDUCTIBILITY trade-off:
    take the surplus as a cheaper amortizing mortgage ADVANCE (whose deductible
    balance is eroded by forced principal repayment) or as a dearer but
    interest-only draw on the revolving LINE (whose deductible balance is not).
    Before #850 the engine priced NEITHER leg's deduction: ``config.cash_out``'s
    only consumers size the invested lump sum (``optimizer.py``,
    ``scipy_optimizer.py``, ``simulate.py``: ``lump_sum = margin_available *
    draw_fraction + cash_out``), and ``apply_margin_heloc_interest`` never
    deducted. So the ranking was decided by the rate gap and capitalization
    alone -- a confident number for a question nobody asked (DP#32).

    ## The two borrowings, and why the split is exactly this

    The lump sum is funded by exactly two borrowings, and
    ``margin_draw_for_lump_sum`` is ALREADY the one rule that says how much of
    it is which (DP#9 -- this reuses it rather than re-deriving the split, so
    the debt booked on the balance sheet and the debt traced for deductibility
    can never disagree):

      - ``margin_draw = margin_draw_for_lump_sum(lump_sum, margin_available)``
        -- the revolving draw, booked as ``SimState.heloc_balance``;
      - ``advance = lump_sum - margin_draw`` -- the remainder, which is the
        mortgage cash-out, already booked ONCE inside ``mortgage_balance`` by
        ``apply_overlay``.

    The two sum to ``lump_sum`` by construction, so no borrowed dollar is
    traced twice (DP#18 -- the trap PR #852 hit from the other side).

    ## Purpose: only the non-registered portion qualifies

    ``StrategyEngine.fill_room`` splits the borrowed lump across RRSP/TFSA/
    FHSA/RESP/non-registered. Only the NON-REGISTERED portion is an
    income-producing use: interest on money borrowed to contribute to a
    registered plan is NOT deductible (the plan's income is sheltered), which
    is exactly the distinction ``_default_heloc_tracing``'s buckets already
    draw and ``compute_heloc_deductible_proportion`` already reads. So the
    borrowed dollars are traced to purpose in ONE proportion,
    ``lump_non_reg / lump_sum``, applied to each leg -- the pro-rata treatment
    CRA applies to a blended borrowing that funds a pooled use (IT-533).

    ## Why the advance's denominator is the WHOLE mortgage

    ``advance_tracing['total_advances']`` is the whole post-refinance
    ``mortgage_balance``, not just the advance: the mortgage is a single
    blended borrowing whose pre-existing balance was borrowed for a personal
    purpose (the house). Its deductible PROPORTION is therefore
    ``investment_advances / mortgage_balance``, fixed for the run -- CRA does
    not let a taxpayer apply repayments preferentially to the personal
    portion, so repayments reduce both portions pro rata. That fixed
    proportion against an AMORTIZING balance is precisely the erosion #849
    names: the deductible balance falls with the principal.

    The revolving leg has no such personal component and does not amortize
    (``apply_margin_heloc_interest`` capitalizes into the charge), so its
    deductible balance persists. That asymmetry is the whole trade-off.

    Args:
        lump_sum: the year-0 lump sum being invested (margin draw + cash-out).
        lump_non_reg: the portion ``fill_room`` allocated to NON-REGISTERED
            investment -- the only income-producing use.
        margin_available: the revolving facility's undrawn room.
        mortgage_balance: the post-refinance mortgage balance (the advance's
            blended denominator).

    Returns:
        ``(advance_tracing, margin_tracing)`` -- two tracing dicts in
        ``_default_heloc_tracing``'s shape, both all-zero (hence a 0.0
        deductible proportion, hence inert) when nothing was borrowed and
        invested. DP#32: a household that took no lump sum gets a hard zero,
        never a fabricated advance.

        ``opening_margin_tracing`` (issue #1039): the trace of a DECLARED
        opening drawn HELOC balance, when the contract carries one. The new
        draw's amounts are ADDED to it rather than replacing it -- a year-0
        lump sum must not silently clobber the historical position's trace
        (DP#32: dropping a declared fact is the founding defect). None (the
        default) means no opening position; the trace is then exactly the
        pre-#1039 all-zero-plus-new-draw dict.
    """
    advance_tracing = _default_heloc_tracing()
    if opening_margin_tracing is None:
        opening_margin_tracing = _default_heloc_tracing()
    # Start from the opening trace so a run with an opening drawn balance but
    # no new margin draw (all of the lump sum went to the advance) carries the
    # historical trace forward unchanged.
    margin_tracing = dict(opening_margin_tracing)
    if lump_sum <= 0:
        return advance_tracing, margin_tracing

    # DP#9: the SAME rule that books the debt decides which borrowing it is.
    margin_draw = margin_draw_for_lump_sum(lump_sum, margin_available)
    advance = lump_sum - margin_draw
    investment_share = lump_non_reg / lump_sum
    registered_share = 1.0 - investment_share

    if margin_draw > 0:
        margin_tracing = _new_heloc_tracing(
            margin_tracing,
            total_advances=(opening_margin_tracing.get('total_advances', 0.0)
                            + margin_draw),
            investment_advances=(
                opening_margin_tracing.get('investment_advances', 0.0)
                + margin_draw * investment_share),
            personal_draws=(opening_margin_tracing.get('personal_draws', 0.0)
                            + margin_draw * registered_share),
        )
    if advance > 0 and mortgage_balance > 0:
        advance_tracing = _new_heloc_tracing(
            advance_tracing,
            # The whole blended mortgage: the advance PLUS the pre-existing,
            # personal-purpose balance the household already owed.
            total_advances=mortgage_balance,
            investment_advances=advance * investment_share,
            personal_draws=mortgage_balance - advance * investment_share,
        )
    return advance_tracing, margin_tracing


def simulate_year_pure(
    state: SimState,
    year: int,
    allocations: Dict[str, float],
    config: SimulationConfig,
    investment_return: Optional[float] = None,
    mortgage_rate: float = 0.05,
    heloc_rate: float = 0.05,
    mortgage_data: Dict = None,
    use_readvanceable: bool = False,
    deduct_later: bool = False,
    # DP#13/26: Round fallback defaults. Callers should always provide actual marginal rates
    # computed from tax brackets. 0.40 is a rough national average, NOT this household's rate.
    primary_marginal_rate: float = 0.40,
    # 0.20 is a round placeholder for lower-bracket spouse rate.
    spouse_marginal_rate: float = 0.20,
    resp_data: List[Dict] = None,
    fhsa_contribution: float = 0.0,
    rrsp_annual_limit: Optional[float] = None,
    tfsa_annual_limit: Optional[float] = None,
    fhsa_annual_limit: Optional[float] = None,
    non_reg_after_tax_return: Optional[float] = None,
    # Issue #641: per-registered-pot foreign-withholding-tax drag derived from
    # each account's OWN declared holdings ({kind: drag_rate} for rrsp/tfsa).
    # None (or an absent kind) preserves the flat gross rate exactly -- the
    # no-op a household with no registered composition relies on (golden).
    registered_wht_drag: Optional[Dict[str, float]] = None,
    # Issue #294: retirement transition. When a member crosses retirement_age,
    # the engine passes the family's government income (already computed from
    # member data + clawback) and a NET drawdown target; the pure step executes
    # the drawdown against state and surfaces everything in YearResult.
    cpp_income: float = 0.0,
    oas_income: float = 0.0,
    pension_income: float = 0.0,
    drawdown_order: Optional[List[str]] = None,
    # RRIF minimum withdrawals (mandatory decumulation once the RRSP is a RRIF,
    # required by age 71 — CRA T4040). The caller supplies the age-based minimum
    # *rate* for each spouse's RRIF (0 before conversion) and the retiree's
    # marginal rate. The pure step forces out max(spending draw already taken,
    # minimum) from each RRIF, taxes the forced excess, and reinvests its
    # after-tax proceeds in the taxable non-reg account (the retiree did not need
    # the cash for spending). Defaults of 0.0 preserve pre-retirement / unit-test
    # behavior exactly.
    rrif_min_rate_primary: float = 0.0,
    rrif_min_rate_spouse: float = 0.0,
    # Net-target drawdown (issues #363/#579 — the only drawdown model; the old
    # blended-rate gross path has been deleted). The spending drawdown fills
    # drawdown_net_target (an after-tax need) via plan_drawdown_net, grossing up
    # only the taxable portion of each source. Default 0.0 preserves
    # pre-retirement / unit-test behavior exactly (no drawdown fires).
    drawdown_net_target: float = 0.0,
    retiree_marginal_rate: float = 0.0,
    # Issue #618: bracket-filling drawdown order. When drawdown_order contains
    # the 'rrsp_bracket_fill' token, the RRSP/RRIF draw against it is capped
    # at max(0, drawdown_bracket_target - drawdown_other_taxable_income)
    # instead of drawn without limit (see plan_drawdown_net). None disables
    # the cap (DP#13 — absence, not a hardcoded opinion), matching every
    # drawdown_order that does not use the bracket-fill token.
    drawdown_bracket_target: Optional[float] = None,
    drawdown_other_taxable_income: float = 0.0,
    # Issue #679: the household's own measured working-phase living-cost
    # budget and this year's after-tax employment income, both required by
    # the cash-flow solvency identity (simulation_rules.apply_solvency).
    # Default 0.0 preserves pre-existing behavior exactly for every caller
    # that does not supply them (DP#16: living_costs<=0 is the module's
    # "not engaged" state -- see that rule's docstring for why this is not
    # a DP#32 zero-as-fallback trap).
    living_costs: float = 0.0,
    after_tax_income: float = 0.0,
    # Issue #679: the portion of THIS year's contributions funded by borrowing
    # (the year-0 leveraged lump sum) rather than by income. See
    # RuleContext.borrowed_investment -- counting the invested borrowing as an
    # outflow without counting the borrowing itself as an inflow invents a
    # shortfall and reports a FALSE ruin on every leveraged strategy.
    borrowed_investment: float = 0.0,
    # Issue #914: the non-borrowed year-0 free cash (RESP-collapse/EAP proceeds)
    # invested this year. Like borrowed_investment it is both an inflow and an
    # outflow in the cash-flow identity (apply_solvency) -- counting the
    # invested proceeds as a contribution outflow without counting the proceeds
    # arriving as an inflow would invent a false shortfall. Unlike
    # borrowed_investment it creates NO debt (it was never borrowed). 0.0 after
    # year 0 and for any run with no free cash (DP#32).
    free_cash_invested: float = 0.0,
    # Issue #137: the year-0 opportunity cost of a declared deployment lag on
    # the refinance cash-out advance, surfaced on the year-0 YearResult so output
    # plugins can render the cost of the delay. 0.0 in every year but year 0
    # and in year 0 for a household that declared no lag (the golden path) --
    # inert, byte-identical (DP#32). The carry is computed by FamilySimulation
    # (which knows the portfolio's year-0 investment return and the declared
    # cash_out -- NOT the borrowing rate, since the debt's interest accrues on
    # the mortgage regardless of the lag) and applied there as a reduction of
    # the deployable principal (capped at the lump for a negative carry,
    # finding #6); this parameter only surfaces it on the result for
    # observability.
    deployment_lag_cost: float = 0.0,
    # Issue #139: the signed NET year-0 LUMP cost of a refinance origination
    # (one-time transaction costs and credits attached to a financial event),
    # surfaced on the year-0 YearResult so output plugins can render the
    # gross-vs-net gap. 0.0 in every year but year 0 and in year 0 for a
    # household that declared no refinance origination lumps (the golden path)
    # -- inert, byte-identical (DP#32). Computed by FamilySimulation from the
    # declared transaction_costs[] entries (costs minus credits, year-0 lumps
    # only, FLOORED at zero so a net credit never inflates the deployable
    # principal above the borrowed lump -- DP#18 money conservation; the
    # excess credit is routed as a year-0 savings cash flow by the adapter)
    # and applied there as a reduction of the deployable principal (the SAME
    # seam #137's deployment-lag carry uses); this parameter only surfaces it
    # on the result for observability.
    transaction_cost_year0: float = 0.0,
    # Issue #343: calendar year for date-computed gates (LIRA→LIF conversion at
    # age 71, LIF min/max factor lookups). `year` is a 0-based projection index;
    # the locked-in-account rules are date-computed from birth_year and therefore
    # need the absolute calendar year, not the index. Callers in the live run
    # loop pass calendar_year=start_year+year. When None, falls back to `year`
    # so direct unit-test callers that already pass a calendar year keep working.
    calendar_year: Optional[int] = None,
    # Issue #758: the retirement-phase flag + effective retirement spending
    # target, forwarded to apply_solvency so it charges the RETIREMENT spending
    # figure in retirement (not the working-phase living_costs) and does not
    # double-count spending the drawdown already funds. Defaults preserve
    # pre-retirement / unit-test behaviour exactly (any_retired=False).
    any_retired: bool = False,
    retirement_spending_target: float = 0.0,
    # Issue #761: True in a working-life year whose income is reduced below
    # the no-override baseline by a dated decisions.income[] shock. When a
    # discretionary split is declared, apply_solvency compresses the
    # discretionary portion of living_costs to zero this year. Default False
    # preserves pre-existing behaviour exactly for every caller that does
    # not supply it (no split declared OR no shock -> full scalar charged).
    income_shock_active: bool = False,
    # ── epic #795 bite 1 (DP#26): inputs for the registered
    # `retirement_income` rule. The fold's prologue used to compute the
    # whole retirement transition inline and pass the OUTPUTS (cpp_income,
    # oas_income, ...) below; now it passes only these INPUTS and the rule
    # produces the outputs onto YearWorkingState. The OUTPUT kwargs (cpp_income,
    # oas_income, drawdown_net_target, any_retired, ...) remain as seed
    # values: simulate_year_pure copies them onto ws BEFORE run_rules so a
    # direct unit-test caller exercising one rule in isolation (passing
    # drawdown_net_target=205_784, say) still works; the retirement_income
    # rule OVERWRITES them when it fires (retirement inputs present), which
    # is the only path the live fold takes. Defaults preserve pre-retirement
    # / direct-unit-test behaviour exactly.
    primary_income_pre: float = 0.0,
    spouse_income_pre: float = 0.0,
    primary_retired: bool = False,
    spouse_retired: bool = False,
    base_primary_income: float = 0.0,
    base_spouse_income: float = 0.0,
    year_brackets: Optional[List[Dict]] = None,
    tax_indexation_rate: float = 0.0,
    # Issue #1020 (S04 Step 1): the prior year's GIS-countable income
    # (retirement income excluding OAS), threaded from the prior YearResult by
    # the live fold's prologue. The retirement_income rule calls gis_benefit on
    # this. None (default) -> GIS stays at its seeded 0.0, byte-identical for
    # every direct unit-test caller and every GIS-ineligible household (DP#32).
    prior_gis_countable_income: Optional[float] = None,
    # Epic #841 bite 2 / issue #812: the strategy's child-allocation targets
    # ({'tfsa','fhsa','rrsp','non_reg': pct}). When provided, each child's OWN
    # income funds contributions into the child's OWN accounts, which grow this
    # year (see _step_child_accounts). None means "do not model children this
    # call" -- the signal the year-0 lump-sum PRE-step (monthly path) and
    # direct unit-test callers use so children are grown exactly ONCE per
    # projection year, by the real per-year fold step, never double-counted.
    child_allocation_pcts: Optional[Dict[str, float]] = None,
    # Epic #841 bite 3: per-child parent->child GIFT funding for this year
    # (aligned to config.children by index), already capped to each child's
    # remaining registered room by child_gift_funding_for_year. Added to each
    # child's own savings inside _step_child_accounts so the gift fills the
    # child's registered room beyond their income. None -> no gifts (the golden
    # household); the child accounts grow on the child's own savings alone,
    # bit-identical to bite 2.
    child_gift_amounts: Optional[list] = None,
    # Issue #859 (Part A): the LOAN-kind (repayable-gift) portion of this year's
    # child funding (aligned to config.children by index), a subset of
    # child_gift_amounts. Accumulated onto each child's loan_funded_principal so
    # the family balance sheet can book it as the lender's receivable / the
    # child's liability (DP#18). None -> no loans; loan_funded_principal stays 0.
    child_loan_amounts: Optional[list] = None,
    # Issue #899 (part a): the precomputed end-of-year OWN RRSP/TFSA for each
    # ADDITIONAL accumulating adult (store slots >= 2), one dict per extra adult
    # from step_extra_adult_accounts (the prologue computes it where income/tax/
    # brackets are available). None/[] for a two-adult household -> nothing is
    # written back over the carried-forward slots -> byte-identical (the golden
    # invariant reads the two-slot YearResult total, not the store).
    extra_adult_accounts: Optional[list] = None,
    # epic #795 bite 3 (DP#26): inputs for the registered `tuition_credit`
    # rule. The prologue used to compute the tuition credit inline (own
    # credit + carry-forward + transfers) and pass POST-credit tax onward;
    # now it passes only these INPUTS and the rule produces the credit onto
    # YearWorkingState. tax_provider is the provider the prologue resolved
    # (DP#20: year-versioned credit rates); None => the rule uses
    # tax_data.default_tax_provider(). The two tax_before scalars are each
    # taxed member's pre-credit tax_on_income (the prologue's per-adult tax
    # loop already computed them). Defaults preserve the no-tuition /
    # direct-unit-test behaviour exactly: when no tuition is declared the
    # rule is a no-op (0 credit, carry-forwards untouched).
    tax_provider: object = None,
    primary_tax_before: float = 0.0,
    spouse_tax_before: float = 0.0,
    # Issue #956 bite B (sale-core): each taxed member's taxable income base,
    # passed to the registered property_disposition rule so a sold property's
    # gain bands against the owner's actual taxable income (DP#9 -- reuses
    # estate.tax_on_capital_gain_at_death's ``other_income`` argument).
    primary_taxable_income: float = 0.0,
    spouse_taxable_income: float = 0.0,
) -> Tuple[YearResult, SimState]:
    """Pure function: advance the simulation by one year.

    DP#26: Same inputs → same outputs. No mutation.
    Reads state, applies year's actions, returns (YearResult, next_state).

    Issue #584/DP#10: the actual government-program logic is a registry of
    19 named rules in ``simulation_rules.py`` (``RULE_ORDER``), each a small
    pure function over an explicit ``YearWorkingState``/``RuleContext`` --
    not inlined here. This function's job is the seam: build the working
    state and context from ``state``/``allocations``/the keyword arguments
    below, fold ``simulation_rules.run_rules`` over the registry (which
    raises loudly if any declared rule has no implementation -- DP#32), and
    assemble the ``YearResult``/``SimState`` from whatever the rules
    produced.

    Issue #25: All Canada-specific fields are read/written through
    jurisdiction_state['canada'] dict, not through SimState top-level fields.

    DP#27: Non-reg investments grow at income-type-specific after-tax rates.
    Registered accounts (RRSP, TFSA, FHSA) grow at the gross rate (tax-sheltered).
    The non_reg_after_tax_return parameter provides the blended after-tax return
    for non-reg investments, computed from portfolio composition and marginal
    tax rate by the caller. When None, falls back to investment_return (flat rate).

    #576: Smith-Manoeuvre investments (``sm_investment_balance``) are
    non-registered/taxable by construction and use this SAME rate to grow
    (see "Grow SM investment" below) -- one model of taxable investing, not
    a separate gross/tax-free shadow copy.

    Args:
        state: Current simulation state
        year: Year index (0-based). Used for the output YearResult.year and
            index-keyed bookkeeping. Date-computed rules (LIRA→LIF conversion,
            LIF withdrawal factors) use calendar_year instead (issue #343).
        calendar_year: Absolute calendar year for this step (e.g. 2050). When
            None, falls back to `year`. The live run loop passes
            start_year + year so the LIRA→LIF conversion gate (age 71) fires.
        allocations: Dict of contribution amounts per account
        config: Simulation configuration (immutable)
        investment_return: Investment return for this year (required; compute from ReturnModel)
        mortgage_rate: Mortgage rate for this year
        heloc_rate: HELOC rate for this year
        mortgage_data: Pre-computed mortgage amortization data for this year
        use_readvanceable: Whether Smith Manoeuvre is active
        deduct_later: Whether to defer RRSP deductions
        primary_marginal_rate: Primary earner's marginal tax rate
        spouse_marginal_rate: Spouse's marginal tax rate
        resp_data: Pre-computed RESP per-child data
        fhsa_contribution: FHSA contribution this year
        fhsa_annual_limit: DP#20 year-specific FHSA dollar limit for this
            simulation year. If None, no annual room is added.
        rrsp_annual_limit: DP#20 year-specific RRSP dollar limit for this
            simulation year. If None, falls back to config.rrsp_annual_max.
        tfsa_annual_limit: DP#20 year-specific TFSA dollar limit for this
            simulation year. If None, falls back to config.tfsa_annual_room_per_person.
        non_reg_after_tax_return: DP#27 income-type-specific after-tax return
            for non-reg investments. If None, falls back to investment_return.

    Returns:
        (YearResult, SimState) — the year's result and the next state.
    """
    # Issue #28: investment_return must be provided explicitly
    if investment_return is None:
        raise ValueError(
            "investment_return is required: pass an explicit rate or compute one "
            "from a ReturnModel (e.g. return_model.return_for_year(year))"
        )

    # Issue #343: resolve the absolute calendar year for date-computed gates.
    # `year` is a 0-based projection index, but the CRI/LIRA→LIF conversion gate
    # (and LIF withdrawal factor lookups) are date-computed from birth_year and
    # need the calendar year. When callers don't supply calendar_year (e.g. unit
    # tests that pass a calendar year directly as `year`), fall back to `year`.
    cal_year = calendar_year if calendar_year is not None else year

    # ── Issue #584/DP#10/DP#26: rules as a registry ──
    # Everything below WAS ~680 lines of inline government-program logic.
    # It is now a fold over `simulation_rules.RULE_ORDER` -- each rule a
    # small, named, independently testable function over explicit state
    # (`YearWorkingState`) and read-only per-call inputs (`RuleContext`).
    # `run_rules` raises loudly if a name in RULE_ORDER has no registered
    # implementation (DP#32: an unregistered rule is a visible failure, not
    # a silent no-op) -- see simulation_rules.py's module docstring and
    # tests/test_issue_584_rules_registry.py for the enforcement this makes
    # possible: an independently-declared expected rule set must match the
    # registry exactly, and a coverage sweep asserts every rule actually
    # fires somewhere in a representative household's trajectory.
    from rule_registry import RuleContext, YearWorkingState
    from simulation_rules import run_rules

    ws = YearWorkingState.from_state(state, allocations, year)
    # Issue #747/#25: normalize the opening canada state to a dict the SAME way
    # YearWorkingState.from_state and the epilogue do, so a None/str canada state
    # (the descriptor contract's degraded input) yields the empty defaults rather
    # than an AttributeError from a raw `.get(...)` on a non-dict.
    _opening_canada = state.jurisdiction_state.get('canada')
    if not isinstance(_opening_canada, dict):
        _opening_canada = {}
    ctx = RuleContext(
        year=year,
        calendar_year=cal_year,
        allocations=allocations,
        config=config,
        investment_return=investment_return,
        mortgage_rate=mortgage_rate,
        heloc_rate=heloc_rate,
        mortgage_data=mortgage_data,
        use_readvanceable=use_readvanceable,
        deduct_later=deduct_later,
        primary_marginal_rate=primary_marginal_rate,
        spouse_marginal_rate=spouse_marginal_rate,
        resp_data=resp_data,
        fhsa_contribution=fhsa_contribution,
        rrsp_annual_limit=rrsp_annual_limit,
        tfsa_annual_limit=tfsa_annual_limit,
        fhsa_annual_limit=fhsa_annual_limit,
        non_reg_after_tax_return=non_reg_after_tax_return,
        registered_wht_drag=registered_wht_drag,
        cpp_income=cpp_income,
        oas_income=oas_income,
        pension_income=pension_income,
        drawdown_order=drawdown_order,
        rrif_min_rate_primary=rrif_min_rate_primary,
        rrif_min_rate_spouse=rrif_min_rate_spouse,
        drawdown_net_target=drawdown_net_target,
        retiree_marginal_rate=retiree_marginal_rate,
        drawdown_bracket_target=drawdown_bracket_target,
        drawdown_other_taxable_income=drawdown_other_taxable_income,
        living_costs=living_costs,
        after_tax_income=after_tax_income,
        borrowed_investment=borrowed_investment,
        # Issue #914: non-borrowed year-0 free cash (RESP proceeds) invested.
        free_cash_invested=free_cash_invested,
        # Issue #758: retirement phase + effective retirement spending target.
        any_retired=any_retired,
        retirement_spending_target=retirement_spending_target,
        income_shock_active=income_shock_active,
        # epic #795 bite 1: inputs for the registered retirement_income rule.
        primary_income_pre=primary_income_pre,
        spouse_income_pre=spouse_income_pre,
        primary_retired=primary_retired,
        spouse_retired=spouse_retired,
        base_primary_income=base_primary_income,
        base_spouse_income=base_spouse_income,
        year_brackets=year_brackets,
        tax_indexation_rate=tax_indexation_rate,
        # Issue #1020 (S04 Step 1): prior-year GIS-countable income for the
        # retirement_income rule's gis_benefit call.
        prior_gis_countable_income=prior_gis_countable_income,
        # Issue #747: opening minimum-tax credit balances (ITA s.120.2 /
        # TP-776.42). Empty for any household that has never paid a minimum tax.
        amt_credit_opening=tuple(_opening_canada.get('amt_credit_buckets', ())),
        qc_imr_credit_opening=tuple(_opening_canada.get('qc_imr_credit_buckets', ())),
        # epic #795 bite 3: inputs for the registered tuition_credit rule.
        tax_provider=tax_provider,
        primary_tax_before=primary_tax_before,
        spouse_tax_before=spouse_tax_before,
        # Issue #956 bite B (sale-core): inputs for the registered
        # property_disposition rule (the owner's taxable income the sold
        # property's gain bands against).
        primary_taxable_income=primary_taxable_income,
        spouse_taxable_income=spouse_taxable_income,
    )
    # epic #795 bite 1: seed ws with the retirement OUTPUT kwargs (defaults
    # 0.0/False/None) BEFORE run_rules so direct unit-test callers that pass
    # e.g. drawdown_net_target=205_784 to exercise a single rule in isolation
    # keep working. The registered retirement_income rule OVERWRITES these
    # when it fires (retirement inputs present) -- the only path the live
    # fold takes. Consumers (retirement_drawdown / rrif_minimum / solvency /
    # the result assembly below) read these off ws, not off ctx.
    ws.cpp_income = cpp_income
    ws.oas_income = oas_income
    ws.pension_income = pension_income
    ws.drawdown_order = drawdown_order
    ws.rrif_min_rate_primary = rrif_min_rate_primary
    ws.rrif_min_rate_spouse = rrif_min_rate_spouse
    ws.drawdown_net_target = drawdown_net_target
    ws.retiree_marginal_rate = retiree_marginal_rate
    ws.drawdown_bracket_target = drawdown_bracket_target
    ws.drawdown_other_taxable_income = drawdown_other_taxable_income
    ws.any_retired = any_retired
    ws.retirement_spending_target = retirement_spending_target
    run_rules(ws, ctx)

    # ── Build new jurisdiction_state (bookkeeping, not itself a rule) ──
    # Issue #837 perf: the prior deepcopy(state.jurisdiction_state) was the
    # biggest CPU sink (~49s / 22.8M calls of a full optimize.py run). It is
    # redundant here: the fold never mutates the prior year's nested objects
    # in place -- the rules read opening_* (read-only) and build fresh new_*
    # values, and the update() below overwrites every nested key (list/dict)
    # in the canada sub-dict with a fresh object. The ONLY thing that must not
    # be shared is the canada DICT OBJECT itself: the post-fold tuition
    # carry-forward write (simulation.py) does
    # next_state.jurisdiction_state['canada']['key'] = ... in place, so the
    # canada dict must be a fresh dict or that write would corrupt the prior
    # year's state. A shallow dict() copy of the top-level jurisdiction_state
    # plus a shallow dict() copy of the canada sub-dict is therefore
    # sufficient and preserves immutability of the prior state:
    #   - the top dict is fresh, so setting ['canada'] below touches only the
    #     new state;
    #   - the canada dict is fresh, so the in-place update() and the post-fold
    #     tuition writes touch only the new state;
    #   - every scalar value is an immutable float/int/str (shared by
    #     reference is safe);
    #   - every nested list/dict value is overwritten by update() (resp_*,
    #     heloc_tracing, spousal_contribution_years, rrsp_ledger) or the
    #     post-fold tuition write (child_tuition_carryforwards) with a fresh
    #     object before the new state is returned, so no shared nested object
    #     survives into the new state. Verified: no rule mutates an opening_*
    #     nested object in place (grep opening_*.append/extend/[..]= -> empty).
    new_jurisdiction_state = dict(state.jurisdiction_state)
    prior_canada = new_jurisdiction_state.get('canada')
    if isinstance(prior_canada, dict):
        new_canada = dict(prior_canada)  # shallow: scalar refs copied, nested
        # objects are shared only until update() replaces them (see above)
    else:
        new_canada = _default_canada_state()
    new_jurisdiction_state['canada'] = new_canada

    _prior_c = prior_canada if isinstance(prior_canada, dict) else {}

    # ── Issue #931: an ADULT first-time home buyer (primary/spouse) ──
    # An adult who becomes a first-time buyer takes their FHSA qualifying
    # withdrawal (the single household FHSA -- ws.new_fhsa_bal / slot 0) TAX-FREE
    # and/or an HBP withdrawal from their OWN RRSP slot (primary=new_rrsp_bal,
    # spouse=new_spouse_rrsp_bal) NON-TAXABLY, both funding the household non-
    # registered cash down payment; the 15-year HBP repayment (tracked in
    # new_canada['adult_hbp']) restores the RRSP. Applied to the ws pots BEFORE
    # the rebuild + build_year_result below so both the carried-forward per-adult
    # stores AND this year's reported totals reflect the withdrawal consistently.
    # The child fold (below) handles a CHILD buyer; the two share the same
    # per-account step (DP#9). Absent any declared adult purchase AND any carried
    # HBP (the golden household) this leaves every ws pot UNTOUCHED (DP#32).
    _adult_ids = [a.get('id', a.get('role')) for a in config.adults()]
    _prior_adult_hbp = _prior_c.get('adult_hbp', {})
    if config.first_home_purchases or _prior_adult_hbp:
        (ws.new_fhsa_bal, _rrsp_by_slot, ws.new_nonreg_bal, ws.new_nonreg_acb,
         _new_adult_hbp, _fhsa_closed) = apply_adult_first_home_purchases(
            _prior_adult_hbp, config.first_home_purchases, _adult_ids,
            fhsa_balance=ws.new_fhsa_bal,
            rrsp_by_slot={0: ws.new_rrsp_bal, 1: ws.new_spouse_rrsp_bal},
            non_reg_balance=ws.new_nonreg_bal, non_reg_acb=ws.new_nonreg_acb,
            calendar_year=cal_year)
        ws.new_rrsp_bal = _rrsp_by_slot[0]
        ws.new_spouse_rrsp_bal = _rrsp_by_slot[1]
        new_canada['adult_hbp'] = _new_adult_hbp
        if _fhsa_closed:
            # A qualifying withdrawal CLOSES the FHSA: no further room/contribution
            # (mirrors the child fold's fhsa_lifetime_remaining=0).
            ws.new_fhsa_room = 0.0
            ws.new_fhsa_lifetime_used = ws.opening_fhsa_lifetime_limit

    new_canada.update({
        # Issue #700/#643: write the two-slot WorkingState scalars back into a
        # FRESH per-adult RRSP store carrying the prior state's adult ids/order.
        # ws.new_spousal_rrsp_room is always 0 (spousal RRSP shares the
        # contributor's room -- simulation_rules.py), so it is not stored.
        'adult_rrsp': rebuild_adult_rrsp(
            _prior_c.get('adult_rrsp', {}),
            primary_own=ws.new_rrsp_bal,
            primary_room=ws.new_rrsp_room,
            spouse_own=ws.new_spouse_rrsp_bal,
            spouse_room=ws.new_spouse_rrsp_room,
            spousal_as_annuitant=ws.new_spousal_rrsp_bal,
        ),
        # Issue #700/#643: write the two-slot WorkingState TFSA scalars back
        # into a FRESH per-adult TFSA store carrying the prior ids/order.
        'adult_tfsa': rebuild_adult_tfsa(
            _prior_c.get('adult_tfsa', {}),
            primary_balance=ws.new_tfsa_p_bal,
            primary_room=ws.new_tfsa_p_room,
            spouse_balance=ws.new_tfsa_sp_bal,
            spouse_room=ws.new_tfsa_sp_room,
        ),
        'resp_balances': ws.new_resp_balances,
        'resp_contributions': ws.new_resp_contributions,
        'resp_cesg': ws.new_resp_cesg,
        'resp_qesi': ws.new_resp_qesi,
        'readvance_heloc_balance': ws.new_sm_heloc,
        'sm_investment_balance': ws.new_sm_investment,
        'sm_investment_cost_basis': ws.new_sm_cost_basis,
        'readvance_total_interest_paid': ws.opening_readvance_total_interest_paid + ws.readvance_interest,
        'readvance_total_tax_saved': ws.opening_readvance_total_tax_saved + ws.readvance_tax_savings,
        'heloc_tracing': ws.new_tracing,
        # Issue #850: the advance's and the drawn line's purpose tracing --
        # fixed at year 0 by the 'borrowing_purpose' rule, carried forward
        # unchanged thereafter. The BALANCES they price move every year; the
        # PURPOSE they were borrowed for does not.
        'advance_tracing': ws.new_advance_tracing,
        'margin_tracing': ws.new_margin_tracing,
        'qc_carry_forward': ws.new_qc_carry_forward,
        'spousal_contribution_years': ws.opening_spousal_contribution_years + ([year] if ws.sp_rrsp_actual > 0 else []),
        'rrsp_ledger': ws.new_ledger.contributions,
        'rrsp_deduction_carry_forward': ws.new_ledger.undeducted_total(),
        'deduct_later_staggered_total': ws.deduct_later_staggered_total,
        'deduct_later_first_claim_income': ws.deduct_later_first_claim_income,
        'deduct_later_total_deducted': ws.deduct_later_total_deducted,
        'heloc_rrsp_paydown': ws.new_heloc_rrsp_paydown,
        # epic #795 bite 3: write the tuition_credit rule's new carry-forwards
        # to jurisdiction_state (the prologue used to write these post-step;
        # the rule now owns them and the epilogue surfaces them). 0.0 / [] for
        # a household that declares no tuition (the golden path).
        'primary_tuition_carryforward': ws.new_primary_tuition_carryforward,
        'spouse_tuition_carryforward': ws.new_spouse_tuition_carryforward,
        'child_tuition_carryforwards': ws.new_child_tuition_carryforwards,
        # Issue #700/#643/#704: write the single-slot WorkingState scalars back
        # into FRESH per-adult FHSA/LIRA/LIF stores carrying the prior ids/order.
        # A second adult's FHSA (slot 1) compounds at the same investment_return
        # the compute grew slot 0 by; a second LIRA/LIF is carried unchanged
        # (2-owner conversion mechanics DEFERRED, Step 4 follow-up).
        'adult_fhsa': rebuild_adult_fhsa(
            _prior_c.get('adult_fhsa', {}),
            balance=ws.new_fhsa_bal,
            room=ws.new_fhsa_room,
            lifetime_used=ws.new_fhsa_lifetime_used,
            lifetime_limit=ws.opening_fhsa_lifetime_limit,
            growth=investment_return,
            # Issue #893: the household FHSA budget slot 0 could not absorb spills
            # into each further owner's OWN FHSA (capped to its own room/lifetime,
            # room re-accruing the annual limit). 0 for a one-owner household.
            overflow=ws.fhsa_overflow,
            annual_limit=fhsa_annual_limit,
        ),
        # DP#16/issue #230: CRI/LIRA and LIF state after growth and conversion
        'adult_lira': rebuild_adult_lira(
            _prior_c.get('adult_lira', {}),
            balance=ws.new_lira_balance,
            birth_year=ws.opening_lira_birth_year,
            jurisdiction=ws.opening_lira_jurisdiction,
            reference_rate=ws.opening_lira_reference_rate,
            conversion_year=ws.opening_lira_conversion_year,
        ),
        'adult_lif': rebuild_adult_lif(
            _prior_c.get('adult_lif', {}),
            balance=ws.new_lif_balance,
            birth_year=ws.new_lif_birth_year,
            jurisdiction=ws.new_lif_jurisdiction,
            reference_rate=ws.new_lif_reference_rate,
        ),
    })

    # Issue #893: the rebuilds above drive slot 0's LIRA/LIF from the single-slot
    # compute and carry any FURTHER owner flat. Now grow + convert each further
    # owner's CRI/LIRA -> LIF at ITS OWN birth-year-driven age (or elected year),
    # jurisdiction and reference rate. A no-op for a household with <=1 locked-in
    # owner (the golden path): slot 0 is left exactly as the compute wrote it.
    new_canada['adult_lira'], new_canada['adult_lif'] = (
        convert_further_adult_locked_in(
            new_canada['adult_lira'], new_canada['adult_lif'],
            calendar_year, investment_return))

    # ── Epic #841 bite 2 / issue #812: grow each child's OWN accounts ──
    # A child is a first-class savings subject (#841): their OWN income funds
    # contributions into their OWN registered accounts, which grow this year
    # exactly as the adults' do. Gated on child_allocation_pcts being provided
    # so children are grown ONCE per projection year, by the real per-year fold
    # step -- the year-0 lump-sum PRE-step (monthly path) and direct unit-test
    # callers pass None and leave the child accounts carried forward untouched
    # (no double-count). When None, new_canada keeps the prior year's list by
    # reference (never mutated in place, like every other carried-forward nested
    # value here). This is bookkeeping, not a household aggregate: the child
    # balances are threaded but NOT summed into total_assets() -- the family
    # objective across all members is a later bite (#841 bite 4).
    if child_allocation_pcts is not None:
        prior_child_accounts = (
            state.jurisdiction_state.get('canada', {}).get('child_accounts', []))
        # Issue #701 Step 6: a child's OWN accounts are funded from the child's
        # AFTER-TAX income (the child is taxed individually on their own return),
        # not the gross the pre-Step-6 fold used. tax_on_income(0)=0 keeps a
        # zero-income child (incl. the golden household) byte-identical.
        # Issue #857: a child's registered room ACCRUES year over year (TFSA at
        # 18+, RRSP from the child's own earned income) -- the accrual half of
        # bite 2's decrement-only room. The dollar limits are the year-versioned
        # figures (DP#20); absent (a direct unit-test caller passing None), fall
        # back to the config scalars exactly as apply_contribution_room does for
        # adults (DP#9). Child room/balances are outside total_assets, so this is
        # a no-op on the golden household's terminal total (its children accrue
        # into their own, separately-threaded accounts only).
        _child_rrsp_limit = (rrsp_annual_limit if rrsp_annual_limit is not None
                             else config.rrsp_annual_max)
        _child_tfsa_limit = (tfsa_annual_limit if tfsa_annual_limit is not None
                             else config.tfsa_annual_room_per_person)
        _tfsa_accrual, _rrsp_accrual = child_room_accrual_for_year(
            config.children, cal_year, config.start_year, config.salary_growth,
            year, _child_rrsp_limit, _child_tfsa_limit,
            config.rrsp_annual_percent)
        new_canada['child_accounts'] = _step_child_accounts(
            prior_child_accounts,
            child_after_tax_savings_for_year(
                config.children, config.savings_rate, config.salary_growth,
                year, year_brackets),
            child_allocation_pcts,
            investment_return,
            child_gift_amounts,
            _tfsa_accrual,
            _rrsp_accrual,
            child_loan_amounts,
        )
        # Issue #704: a child who becomes a first-time home buyer this calendar
        # year makes a tax-free FHSA qualifying withdrawal + a non-taxable HBP
        # RRSP withdrawal (both toward the down payment), and any open HBP repays
        # its scheduled amount. Absent a declared purchase (the golden household)
        # this returns the stepped accounts UNCHANGED (DP#32) -- no-op.
        new_canada['child_accounts'] = apply_child_first_home_purchases(
            new_canada['child_accounts'], config.children,
            config.first_home_purchases, cal_year)

    # ── Issue #899 (part a): write each ADDITIONAL accumulating adult's OWN
    # end-of-year RRSP/TFSA into the per-adult store (slots >= 2) ──
    # rebuild_adult_rrsp/tfsa above carried these slots FORWARD at their opening
    # balances (the two-slot compute drives only slots 0/1); the prologue's
    # precomputed accumulation (each adult's OWN after-tax income -> their OWN
    # RRSP/TFSA, grown -- step_extra_adult_accounts) replaces them here. None/
    # empty for a two-adult household -> no store write -> byte-identical.
    if extra_adult_accounts:
        _rrsp_store = dict(new_canada['adult_rrsp'])
        _tfsa_store = dict(new_canada['adult_tfsa'])
        for _e in extra_adult_accounts:
            _aid = _e['id']
            _rrsp_store[_aid] = {'own': _e['rrsp_balance'],
                                 'own_room': _e['rrsp_room'],
                                 'spousal_as_annuitant': 0.0}
            _tfsa_store[_aid] = {'balance': _e['tfsa_balance'],
                                 'room': _e['tfsa_room']}
        new_canada['adult_rrsp'] = _rrsp_store
        new_canada['adult_tfsa'] = _tfsa_store

    # ── Issue #747: persist the closing minimum-tax credit balances ──
    # The `amt` rule wrote the surviving AMT / Quebec-IMR credits (after this
    # year's expiry, recovery and booking) to ws; carry them into next year's
    # state (ITA s.120.2 / TP-776.42). Empty in, empty out for the golden
    # household (its fast no-op leaves the ws defaults untouched).
    new_canada['amt_credit_buckets'] = list(ws.amt_credit_closing)
    new_canada['qc_imr_credit_buckets'] = list(ws.qc_imr_credit_closing)

    # ── Build new state ──
    # Issue #679: emergency_reserve_balance and heloc_balance are read from
    # ws.new_* AFTER the 'solvency' rule (last in RULE_ORDER) may have drawn
    # against them -- same principle as every other account below, whose
    # ws.new_* value already reflects any forced liquidation this year.
    new_state = SimState(
        non_reg_balance=ws.new_nonreg_bal,
        non_reg_acb=ws.new_nonreg_acb,
        mortgage_balance=ws.new_mortgage_balance,
        heloc_balance=ws.new_heloc_balance,
        emergency_reserve_balance=ws.new_emergency_reserve,
        # issue #936: carry the promo-deposit balance forward -- grown this year
        # by apply_deposit_product_growth at its rate_schedule, net of interest
        # tax. 0.0 for a household with no taken product (DP#32).
        deposit_product_balance=ws.new_deposit_product_balance,
        credit_facility_balance=ws.new_credit_facility_balance,
        # issue #763: carry each consumer loan's end-of-year balance
        # forward into the next fold step (amortized by apply_consumer_loans).
        consumer_loan_balances=ws.new_consumer_loan_balances,
        # issue #759: carry each installment plan's end-of-year remaining-
        # payment balance forward into the next fold step (drained by
        # apply_installments; reaches 0 the year after the final payment).
        installment_balances=ws.new_installment_balances,
        # issue #692: the couple's non-principal property net equities join the
        # annual balance sheet. issue #696: recomputed for THIS calendar year so
        # a property bought mid-horizon contributes zero until its purchase year
        # and its net_equity from then on (_property_equity_for_year); a property
        # held from year 0 has a constant net_equity, so this is byte-identical
        # to carrying #692's static figure forward (DP#32).
        property_equities=[
            _property_equity_for_year(prop, cal_year, config.start_year)
            for prop in config.properties],
        # Issue #967: carry each financed property's mid-horizon mortgage
        # end-of-year balance forward into the next fold step (amortized by
        # apply_second_property_mortgage from the precomputed schedule; reaches
        # 0 at the payoff year). The list is parallel to config.properties by
        # index; an all-zero list for a household with no financed property
        # (the golden path) -> total_debt unchanged (DP#32).
        second_property_mortgage_balances=ws.new_second_property_mortgage_balances,
        jurisdiction_state=new_jurisdiction_state,
    )

    # ── Build YearResult ──
    total_rrsp = ws.new_rrsp_bal + ws.new_spousal_rrsp_bal + ws.new_spouse_rrsp_bal
    total_tfsa = ws.new_tfsa_p_bal + ws.new_tfsa_sp_bal
    resp_total = sum(ws.new_resp_balances)
    # issue #692: the couple's non-principal real estate's net equity joins the
    # annual balance sheet. issue #696: valued for THIS calendar year so a
    # mid-horizon purchase's equity appears in its purchase year (not before) --
    # the same year its down payment is drawn from the portfolio by the solvency
    # waterfall (simulation_rules.apply_solvency), so the down-payment dollars
    # move from the asset side to equity. Empty for a household with only a
    # principal residence (the golden path) -> no change to total_assets (DP#32).
    property_equity_total = sum(
        _property_equity_for_year(prop, cal_year, config.start_year)
        for prop in config.properties)
    total_assets = total_rrsp + total_tfsa + resp_total + ws.new_nonreg_bal + ws.new_fhsa_bal + (ws.new_sm_investment if use_readvanceable else 0) + ws.new_lira_balance + ws.new_lif_balance + ws.new_emergency_reserve + ws.new_deposit_product_balance + property_equity_total
    total_heloc_all = ws.new_sm_heloc + ws.new_heloc_balance
    # Issue #689: the credit facility is a SEPARATE liability from the HELOC
    # (never merged -- see SimulationConfig.credit_facility_limit's
    # docstring), but it is still real household debt once drawn.
    # Issue #763: closed-end consumer loans are real household debt too --
    # unsecured, but a liability the household owes and services. Folded in
    # so total_debt / net_assets see the whole balance sheet, not a picture
    # with the car loan erased.
    consumer_loan_balance = sum(ws.new_consumer_loan_balances)
    # Issue #967: the outstanding balances of mid-horizon mortgages
    # originated by properties' `purchase.financing` -- real household debt
    # once originated, serviced (principal + interest) from the purchase year
    # to payoff by the `second_property_mortgage` rule. Folded into total_debt
    # so the balance sheet sees the whole liability, mirroring the consumer
    # loans above (an amortizing secured debt is the same kind of liability an
    # amortizing unsecured one is). All-zero for a household with no financed
    # property (the golden path) -> total_debt unchanged (DP#32).
    second_property_mortgage_balance = sum(ws.new_second_property_mortgage_balances)
    total_debt = (ws.new_mortgage_balance + total_heloc_all
                  + ws.new_credit_facility_balance + consumer_loan_balance
                  + second_property_mortgage_balance)
    # Issue #759: installment plans are deliberately NOT in total_debt -- an
    # installment plan is a committed payment schedule for services already
    # received, not a callable borrowing against the estate. The remaining-
    # payment balance is reported on YearResult.installment_balance for
    # transparency, and the year's PAYMENT is folded into the solvency
    # debt-service term (apply_solvency) where it affects the cash-flow
    # identity and runway -- not the balance-sheet net_assets figure.
    installment_balance = sum(ws.new_installment_balances)

    # ── Retirement income totals (issue #294) ──
    # epic #795 bite 1: read the government-income components off ws (the
    # registered retirement_income rule wrote them there; or the seeded kwarg
    # defaults for a direct unit-test caller), not off the kwargs.
    # Issue #1020 (S04 Step 1): GIS is now part of the household's retirement
    # income. It is non-taxable (not in the tax base) but it IS cash the
    # household receives, so it belongs in ``retirement_income`` (the
    # total-government-income figure) and ``total_family_income``. It is NOT
    # in the drawdown shortfall base (``covered_net`` already folded it in,
    # inside the retirement_income rule) and NOT in the tax-bracket stack.
    employment_income = allocations.get('_primary_income', 0) + allocations.get('_spouse_income', 0)
    # Issue #302: LIF mandatory withdrawal is taxable retirement income, separate
    # from drawdown. The drawdown draws from accounts AFTER the mandatory LIF
    # withdrawal has already been taken, so there's no double-counting.
    retirement_income = ws.cpp_income + ws.oas_income + ws.pension_income + ws.gis_income + ws.drawdown_total + ws.lif_withdrawal
    # total_family_income spans employment (pre-retirement) plus government
    # benefits and drawdown (retirement). For pure pre-retirement horizons all
    # retirement components are 0, so this equals the historical employment sum.
    total_family_income = employment_income + ws.cpp_income + ws.oas_income + ws.pension_income + ws.gis_income + ws.drawdown_total + ws.lif_withdrawal

    result = YearResult(
        year=year + 1,
        mortgage_rate=mortgage_rate,
        heloc_rate=heloc_rate,
        primary_income=allocations.get('_primary_income', 0),
        spouse_income=allocations.get('_spouse_income', 0),
        total_family_income=total_family_income,
        annual_savings=allocations.get('_annual_savings', 0),
        contributions={k: v for k, v in allocations.items() if not k.startswith('_')},
        # Issue #170: the declared-but-refused RRSP contributions (over-room
        # slices the clamp declined) travel on every year's output, so a
        # household whose manual split exceeds room SEES the refusal instead
        # of silently booking $0.
        rrsp_contribution_refused_own=ws.rrsp_refused_own,
        rrsp_contribution_refused_spousal=ws.rrsp_refused_spousal,
        primary_rrsp=ws.new_rrsp_bal,
        spousal_rrsp=ws.new_spousal_rrsp_bal,
        spouse_rrsp=ws.new_spouse_rrsp_bal,
        total_rrsp=total_rrsp,
        primary_tfsa=ws.new_tfsa_p_bal,
        spouse_tfsa=ws.new_tfsa_sp_bal,
        total_tfsa=total_tfsa,
        resp_balance=resp_total,
        resp_eap_paid=ws.resp_eap_paid,
        resp_pse_paid=ws.resp_pse_paid,
        resp_aip_tax=ws.resp_aip_tax,
        non_reg_balance=ws.new_nonreg_bal,
        non_reg_acb=ws.new_nonreg_acb,
        non_reg_unrealized_gains=ws.new_nonreg_bal - ws.new_nonreg_acb,
        # Issue #754: the year's realized capital gain (proceeds - ACB, 100%) --
        # the retirement-drawdown disposition plus any forced solvency-waterfall
        # non-reg liquidation. The taxable (50%-included) slice already lives in
        # drawdown_taxable / forced_liquidation_tax; this is the pre-inclusion
        # figure the year-end AMT base (#710) reads.
        # Issue #956 bite B (sale-core): also folds in the realized gain from a
        # declared mid-horizon property SALE (ws.sale_realized_gain), so the AMT
        # base sees the property disposition's realized gain too.
        realized_capital_gains=(ws.drawdown_realized_capital_gain
                                + ws.solvency_realized_gain
                                + ws.sale_realized_gain
                                + ws.principal_sale_realized_gain
                                + ws.heloc_servicing_realized_gain),
        # Issue #956 bite B (sale-core): the net proceeds invested into non-reg
        # this year from declared property SALES, and the disposition tax those
        # sales crystallized (already netted out of the proceeds). Surfaced for
        # transparency; NOT added to the ordinary income-tax base (computed once).
        sale_proceeds_invested=ws.sale_proceeds_invested,
        sale_disposition_tax=ws.sale_disposition_tax,
        # Issue #956 bite E (principal-residence disposition): the net
        # proceeds invested into non-reg from a declared SALE of the PRINCIPAL
        # residence, the PRE-apportioned disposition tax (already netted out
        # of P_net), the pre-inclusion realized gain (folded into
        # realized_capital_gains above for the AMT base), and the secured debt
        # discharged at the sale. Surfaced for transparency (DP#32); NOT added
        # to the ordinary income-tax base (computed once, in the rule).
        principal_sale_proceeds_invested=ws.principal_sale_proceeds_invested,
        principal_sale_disposition_tax=ws.principal_sale_disposition_tax,
        principal_sale_realized_gain=ws.principal_sale_realized_gain,
        principal_sale_discharged_debt=ws.principal_sale_discharged_debt,
        # Issue #710: the year-end AMT surcharge the `amt` rule assessed and
        # charged (0.0 whenever the fold realized no capital gain -- every year
        # AMT cannot bite in this engine, #754). The rule already reduced
        # ws.new_nonreg_bal by the funded amount, so total_assets reflects the
        # charge without a separate subtraction here.
        amt_surcharge=ws.amt_surcharge,
        # Issue #747: the QC IMR surcharge and the minimum-tax credit bookkeeping
        # (recovered this year + closing federal balance) surfaced for
        # transparency. All 0.0 whenever the fold assesses no minimum tax and
        # carries no credit (the golden household).
        qc_imr_surcharge=ws.qc_imr_surcharge,
        amt_credit_recovered=ws.amt_credit_recovered,
        qc_imr_credit_recovered=ws.qc_imr_credit_recovered,
        amt_credit_balance=sum(c.amount for c in ws.amt_credit_closing),
        # Issue #1082: the assessed net minimum-tax charge and the slice of it
        # the non-reg pot could not fund -- reported, never absorbed (DP#32).
        amt_net_charge=ws.amt_net_charge,
        amt_unfunded=ws.amt_unfunded,
        lira_balance=ws.new_lira_balance,
        lif_balance=ws.new_lif_balance,
        lif_withdrawal=ws.lif_withdrawal,
        cpp_income=ws.cpp_income,
        oas_income=ws.oas_income,
        # Issue #1033: the drawdown + forced-RRIF-minimum OAS recovery tax for
        # the year (the preliminary clawback on the CPP+pension base, booked in
        # ``retirement_income`` before ``sm_interest`` runs, is already netted
        # out of ``oas_income`` above and is NOT included here). Computed as
        # the gross OAS the drawdown priced against minus the net OAS left after
        # every clawback booking, so it captures exactly the slice the s.20(1)
        # (c) deduction's routing through ``drawdown_other_taxable_income``
        # moves. ``drawdown_oas_gross`` is itself net of the preliminary
        # recovery tax, so this is non-negative and 0.0 in every pre-retirement
        # year (no OAS). See ``YearResult.oas_clawback`` for the contract.
        oas_clawback=max(0.0, ws.drawdown_oas_gross - ws.oas_income),
        pension_income=ws.pension_income,
        # Issue #1020 (S04 Step 1): GIS paid this year (0.0 pre-retirement /
        # GIS-ineligible; folded into retirement_income/total_family_income
        # above and into the drawdown covered_net inside the retirement_income
        # rule). Surfaced so a test/optimizer can observe it directly.
        gis_income=ws.gis_income,
        drawdown_income=ws.drawdown_total,
        drawdown_taxable=ws.drawdown_taxable,
        drawdown_net_target=ws.drawdown_net_target,
        drawdown_net_delivered=ws.drawdown_net_delivered,
        drawdown_shortfall=ws.drawdown_shortfall,
        retirement_income=retirement_income,
        employment_income=employment_income,
        # Issue #758: retirement-phase flag, for the runway metric to scope
        # itself to working life (and for the solvency identity's retirement
        # branch -- see apply_solvency). epic #795 bite 1: read off ws
        # (retirement_income rule wrote it; or the seeded default).
        any_retired=ws.any_retired,
        total_assets=total_assets,
        mortgage_balance=ws.new_mortgage_balance,
        heloc_balance=total_heloc_all,
        total_debt=total_debt,
        # Issue #1031: surface the SM sleeve so the estate's deemed disposition
        # can read the terminal FMV / ACB / HELOC debt off the YearResult, and
        # so a test can observe them directly. Gated on use_readvanceable like
        # total_assets above (a non-readvanceable run carries no SM sleeve);
        # 0.0 for the golden household -> inert (DP#32).
        sm_investment_balance=(ws.new_sm_investment if use_readvanceable else 0.0),
        sm_investment_cost_basis=(ws.new_sm_cost_basis if use_readvanceable else 0.0),
        sm_heloc_balance=(ws.new_sm_heloc if use_readvanceable else 0.0),
        # Issue #1017: the SM unwind's proceeds/tax/heloc_repaid/net_delivered,
        # surfaced for transparency (DP#32). 0.0 in every year no unwind fires.
        sm_unwind_proceeds=ws.sm_unwind_proceeds,
        sm_unwind_tax=ws.sm_unwind_tax,
        sm_unwind_heloc_repaid=ws.sm_unwind_heloc_repaid,
        sm_unwind_net_delivered=ws.sm_unwind_net_delivered,
        primary_marginal=primary_marginal_rate,
        spouse_marginal=spouse_marginal_rate,
        bracket_gap=primary_marginal_rate - spouse_marginal_rate,
        rrsp_tax_savings=ws.rrsp_deduction_savings + ws.spouse_deduction_savings if deduct_later else (ws.p_rrsp_actual + ws.s_rrsp_actual) * primary_marginal_rate + ws.sp_rrsp_actual * spouse_marginal_rate,
        deduction_claims=ws.deduction_claims,
        deduction_advantage_vs_now=ws.deduction_advantage_vs_now,
        readvance_interest=ws.readvance_interest,
        readvance_tax_savings=ws.readvance_tax_savings,
        # Issue #1083: the deduction's statutory saving on the retired
        # primary's prologue-taxed rental/loan slice (0.0 in accumulation and
        # whenever the deduction fits inside the drawdown base).
        sm_interest_nondrawdown_tax_saving=ws.sm_interest_nondrawdown_tax_saving,
        sm_qc_deductible=ws.qc_deductible,
        sm_qc_carry_forward=ws.new_qc_carry_forward,
        sm_deductible_proportion=ws.deductible_proportion,
        # Issue #850: the two legs of #849's trade-off, priced at last.
        advance_deductible_balance=ws.advance_deductible_balance,
        advance_deductible_interest=ws.advance_deductible_interest,
        margin_deductible_balance=ws.margin_deductible_balance,
        margin_deductible_interest=ws.margin_deductible_interest,
        traced_borrowing_tax_savings=ws.traced_borrowing_tax_savings,
        mortgage_payment=ws.mort.get('total_payment', 0),
        mortgage_interest=ws.mort.get('total_interest', 0),
        mortgage_principal=ws.principal_paid,
        # issue #681: what the charge ACTUALLY allowed, not what the mortgage
        # happened to pay down. These used to be the same number
        # (``ws.principal_paid``) because the readvance was unbounded; they
        # diverge the moment the charge fills up, and that divergence is the
        # whole point -- ``readvance_blocked`` is principal the household
        # repaid but could NOT re-borrow, reported rather than silently
        # dropped (DP#32).
        sm_readvanced=ws.sm_readvanced,
        readvance_room=ws.readvance_room,
        readvance_blocked=ws.readvance_blocked,
        # issue #681: HELOC interest the charge had no room to capitalize is
        # paid in CASH out of the household's investments -- a real cost, and
        # a large part of why "borrow the maximum" is not free. Reported, not
        # absorbed.
        heloc_interest_capitalized=ws.margin_heloc_interest_capitalized,
        heloc_interest_serviced=ws.margin_heloc_interest_serviced,
        heloc_interest_unfunded=ws.heloc_interest_unfunded,
        # Issue #1069: the charged total and the funded slice, so the
        # conservation identity (charged = capitalized + serviced =
        # capitalized + funded + unfunded) is checkable from the outside by
        # the run-path invariant instead of being taken on faith (DP#32).
        heloc_interest_charged=ws.margin_heloc_interest,
        heloc_servicing_funded=ws.heloc_servicing_funded,
        heloc_servicing_realized_gain=ws.heloc_servicing_realized_gain,
        heloc_servicing_tax=ws.heloc_servicing_tax,
        # Issue #679: solvency identity + forced-liquidation waterfall.
        after_tax_income=ws.solvency_after_tax_income,
        living_costs=ws.solvency_living_costs,
        solvency_spending_outflow=ws.solvency_spending_outflow,
        solvency_discretionary_compressed=ws.solvency_discretionary_compressed,
        # Issue #760: this year's dated living-cost segment outflow charged in
        # the solvency identity (on top of living_costs), net of any
        # discretionary segment compressed under an income shock.
        expense_segment_outflow=ws.expense_segment_outflow,
        debt_service=ws.solvency_debt_service,
        contributions_total=ws.solvency_contributions,
        solvency_shortfall=ws.solvency_shortfall,
        solvency_covered=ws.solvency_covered,
        forced_liquidation_tax=ws.solvency_tax_paid,
        forced_liquidation_realized_loss=ws.solvency_realized_loss,
        forced_liquidation_events=ws.solvency_liquidations,
        # Issue #688: the reserve, its target, and the gap between them.
        emergency_reserve_balance=ws.new_emergency_reserve,
        emergency_reserve_target=ws.emergency_reserve_target,
        emergency_reserve_months_covered=ws.emergency_reserve_months_covered,
        # Issue #689: the revolving credit facility (line_of_credit) --
        # $0/undrawn unless a real #679 shortfall reached it.
        credit_facility_balance=ws.new_credit_facility_balance,
        # True on a shortfall year where no line_of_credit was DECLARED at
        # all -- the household's real resilience may be understated by an
        # undeclared real-world facility, a fact about the input, not (post
        # #689) a structural gap in the engine (DP#32).
        credit_facility_unrepresentable=ws.solvency_credit_facility_unrepresentable,
        # Issue #763: closed-end consumer loans -- the total balance (folded
        # into total_debt above) and this year's payment / interest.
        consumer_loan_balance=consumer_loan_balance,
        consumer_loan_payment=ws.consumer_loan_payment,
        consumer_loan_interest=ws.consumer_loan_interest,
        # Issue #759: fixed-term installment obligations -- the remaining-
        # payment balance (a reporting figure, NOT in total_debt above) and
        # this year's payment (the installment half of the solvency
        # debt-service term, 0% interest so no interest component). The
        # payment is 0 in every year the plan is not yet active or has ended.
        installment_balance=installment_balance,
        installment_payment=ws.installment_payment,
        # Issue #967: mid-horizon mortgages -- the total balance (folded into
        # total_debt above), this year's payment / interest, and the principal
        # originated this year (an inflow in the purchase year). All 0 for a
        # household with no financed property (the golden path) (DP#32).
        second_property_mortgage_balance=second_property_mortgage_balance,
        second_property_mortgage_payment=ws.second_property_mortgage_payment,
        second_property_mortgage_interest=ws.second_property_mortgage_interest,
        second_property_mortgage_originated=ws.second_property_mortgage_originated,
        ruined=ws.solvency_ruined,
        # epic #841 bite 4: carry the end-of-year child-account snapshot onto
        # the result so the family objective can value every member's wealth.
        # new_canada shallow-copied it forward from the prior year (or the
        # step above replaced it this year); empty/all-zero for a household
        # with no child-savers, so total_assets() is untouched (DP#32).
        child_accounts=new_canada.get('child_accounts', []),
        # Issue #899 (part a): the additional accumulating adults' OWN end-of-
        # year RRSP/TFSA, surfaced as reporting data (like child_accounts) so the
        # family objective values them without them entering the two-slot
        # total_assets. Empty for a two-adult household (DP#32).
        extra_adult_accounts=extra_adult_accounts or [],
        # epic #795 bite 3: surface the tuition_credit rule's end-of-year
        # carry-forwards (the prologue used to set these post-step; the rule
        # now owns them and build_year_result surfaces them). 0.0 for a
        # household that declares no tuition (the golden path).
        primary_tuition_carryforward=ws.new_primary_tuition_carryforward,
        spouse_tuition_carryforward=ws.new_spouse_tuition_carryforward,
        # Issue #137: surface the year-0 deployment-lag carry cost (computed
        # by FamilySimulation and passed through here) so output plugins can
        # render it. 0.0 in every year but year 0 (DP#32).
        deployment_lag_cost=deployment_lag_cost,
        # Issue #139: surface the year-0 net refinance-origination
        # transaction cost/credit (computed by FamilySimulation and passed
        # through here) so output plugins can render the gross-vs-net gap.
        # 0.0 in every year but year 0 (DP#32).
        transaction_cost_year0=transaction_cost_year0,
    )

    return result, new_state
