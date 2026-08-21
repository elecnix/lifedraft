#!/usr/bin/env python3
"""Canada-specific simulation state.

Per DP#26: jurisdiction-specific state goes into SimState.jurisdiction_state['canada'],
not as individual fields on SimState. This module defines CanadaSimState, which holds
all Canada-specific state that the core simulation engine treats as opaque data.

This module also defines Canada-specific dataclasses that were previously in
simulation_state.py (HelocTracingState, QcDeductionState). Issue #25 moves them
here to enforce DP#25: core has no imports from countries.canada.

References:
    DESIGN_PRINCIPLES.md — Jurisdiction State (DP#9)
    countries/canada/provinces/quebec/quebec_deduction.py — QuebecDeductionTracker
    simulation_state.py — RRSPListLedger (the canonical per-contribution ledger)
"""

from dataclasses import dataclass, field
from typing import List

from countries.canada.provinces.quebec.quebec_deduction import QuebecDeductionTracker
from simulation_state import RRSPListLedger


@dataclass
class HelocTracingState:
    """Serializable snapshot of HELOC tracing state (ITA §20(1)(c))."""
    total_advances: float = 0.0
    investment_advances: float = 0.0
    rrsp_advances: float = 0.0
    tfsa_advances: float = 0.0
    personal_draws: float = 0.0


@dataclass
class QcDeductionState:
    """Serializable snapshot of Quebec deduction carry-forward state."""
    carry_forward: float = 0.0


@dataclass
class CanadaSimState:
    """Canada-specific state carried in SimState.jurisdiction_state['canada'].

    Fields:
        adult_rrsp: Per-adult RRSP store (issue #700/#643), keyed by adult id in
            canonical order: {adult_id: {'own', 'own_room', 'spousal_as_annuitant'}}.
            Replaces the three hardcoded pots (rrsp/spouse_rrsp/spousal_rrsp) --
            each adult's own RRSP, own room, and any spousal RRSP they annuit.
        adult_tfsa: Per-adult TFSA store (issue #700/#643), keyed by adult id in
            canonical order: {adult_id: {'balance', 'room'}}. Replaces the two
            hardcoded pots (tfsa_primary/tfsa_spouse).
        resp_balances: Per-child RESP balances
        readvance_heloc_balance: Smith Manoeuvre HELOC balance
        sm_investment_balance: Smith Manoeuvre non-reg investment balance
        sm_investment_cost_basis: Smith Manoeuvre investment cost basis
        readvance_total_interest_paid: Cumulative SM interest paid
        readvance_total_tax_saved: Cumulative SM tax savings
        heloc_tracing: HELOC tracing state for deductible proportion
        qc_deduction: Quebec interest deduction carry-forward tracker
        spousal_contribution_years: Years with spousal RRSP contributions (ITA s.146(8.3))
        rrsp_ledger: Per-contribution RRSP deduction tracking (DP#19)
        rrsp_deduction_carry_forward: Unused RRSP deduction room (DP#45)
        heloc_rrsp_paydown: Cumulative RRSP refund applied to HELOC paydown
        adult_fhsa: Per-adult FHSA store (issue #700/#643/#704), keyed by adult
            id in canonical order: {adult_id: {'balance', 'room',
            'lifetime_used', 'lifetime_limit'}}. Replaces the singleton FHSA pot.
        adult_lira: Per-adult CRI/LIRA store (issue #700/#643), keyed by adult
            id: {adult_id: {'balance', 'birth_year', 'jurisdiction',
            'reference_rate', 'conversion_year'}}. Replaces the singleton LIRA.
        adult_lif: Per-adult LIF store (issue #700/#643), keyed by adult id:
            {adult_id: {'balance', 'birth_year', 'jurisdiction',
            'reference_rate'}}. Replaces the singleton LIF.
    """

    # RRSP accounts (issue #700/#643): per-adult store, not three flat pots.
    adult_rrsp: dict = field(default_factory=dict)

    # TFSA accounts (issue #700/#643): per-adult store, not two flat pots.
    adult_tfsa: dict = field(default_factory=dict)

    # RESP (per-child balances)
    resp_balances: List[float] = field(default_factory=list)

    # Smith Manoeuvre
    readvance_heloc_balance: float = 0.0
    sm_investment_balance: float = 0.0
    sm_investment_cost_basis: float = 0.0
    readvance_total_interest_paid: float = 0.0
    readvance_total_tax_saved: float = 0.0

    # HELOC tracing (DP#6, ITA §20(1)(c))
    heloc_tracing: HelocTracingState = field(default_factory=HelocTracingState)

    # Quebec deduction carry-forward
    qc_deduction: QuebecDeductionTracker = field(default_factory=QuebecDeductionTracker)

    # Spousal RRSP attribution tracking (ITA s.146(8.3))
    spousal_contribution_years: List[int] = field(default_factory=list)

    # RRSP per-contribution deduction ledger (DP#19). Uses the canonical
    # simulation_state.RRSPListLedger; the dead countries.canada.rrsp_ledger
    # clone was removed (#744, DP#9).
    rrsp_ledger: RRSPListLedger = field(default_factory=RRSPListLedger)

    # Deduct-later carry-forward (DP#45)
    rrsp_deduction_carry_forward: float = 0.0

    # Cumulative RRSP refund applied to HELOC paydown
    heloc_rrsp_paydown: float = 0.0

    # FHSA / CRI-LIRA / LIF (issue #700/#643/#704): per-adult stores, not
    # singleton household pots. See the class docstring for each entry's shape.
    adult_fhsa: dict = field(default_factory=dict)
    adult_lira: dict = field(default_factory=dict)
    adult_lif: dict = field(default_factory=dict)

    def __post_init__(self):
        """Ensure rrsp_ledger is initialized (handles None from explicit init)."""
        if self.rrsp_ledger is None:
            self.rrsp_ledger = RRSPListLedger()
