"""Canada-specific state accessors extracted from simulation_state (issue #237).

This module holds the Canada-only plumbing that simulation_state.py no longer
owns (DP#9: no re-export shim; DP#25: one-directional dependency; DP#26: pure
helpers). simulation_state.py imports the names it still uses directly from here.
"""

from typing import Tuple

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

# ── Default values for Canada-specific jurisdiction_state keys ──────────────
# These defaults are used by __post_init__ and initial() when constructing
# the jurisdiction_state['canada'] dict. They are NOT imports from
# countries.canada (DP#25).

# Issue #277: the ``jurisdiction_state['canada']`` key that carries the CLOSING
# year's GIS-countable income into the next year (see the entry in
# ``_default_canada_state`` below). ``simulation_state.simulate_year_pure`` is
# its only writer and its only reader, and both spell it through this one
# constant, so a read/write spelling drift cannot turn it into a dead write.
GIS_COUNTABLE_INCOME_KEY = 'gis_countable_income'


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

        # Issue #277: the CLOSING year's GIS-countable income (everything the
        # household received except OAS and GIS, CRA's GIS income test), which
        # next year's step reads as its PRIOR-year base. simulate_year_pure
        # writes a float here on every step and reads it back at the next
        # step's open, so the value crosses years inside SimState and every
        # fold (run, _run_monthly, the Grid/Scipy/Monte-Carlo optimizer, DP)
        # sees it. None on a state that has not been stepped yet: "no prior
        # year recorded", so the retirement_income rule pays no GIS (DP#32:
        # absence is never coerced to $0 of income, which would pay FULL GIS).
        GIS_COUNTABLE_INCOME_KEY: None,

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

        # Issue #140: the capital-loss carry-forward pool, in TAXABLE-BASIS
        # (includable) dollars -- a $40k raw net capital loss at 50% inclusion
        # is $20k here. Settled once per year by the registered `capital_loss`
        # rule (same-year offset, carry-forward; NEVER against ordinary
        # income); its cash value is realized at pricing time, in the
        # retirement drawdown's lead tax-free slice. 0.0 for a household that
        # has never realized a net capital loss (inert, DP#32). Lives in
        # jurisdiction_state['canada'] because it is a Canada-specific tax
        # construct (DP#25: no Canada fields at SimState top level).
        'capital_loss_carryforward': 0.0,

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



def _default_heloc_tracing() -> dict:
    """Return a fresh HELOC tracing dict with zero values."""
    return {
        'total_advances': 0.0,
        'investment_advances': 0.0,
        'rrsp_advances': 0.0,
        'tfsa_advances': 0.0,
        'personal_draws': 0.0,
    }

