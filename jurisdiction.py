#!/usr/bin/env python3
"""
Jurisdiction Adapters — Protocol interfaces for jurisdiction-agnostic simulation (DP#8, DP#25).

Core simulation (simulation.py, simulation_state.py) depends only on these
Protocol interfaces, never on country-specific packages directly. Each
jurisdiction package (e.g., countries/canada/) provides an adapter module
that implements these protocols.

DP#8: Compose through data, not through inheritance. Jurisdiction-specific
state lives in SimState.jurisdiction_state as opaque data; core passes it
through without interpreting it.

DP#25: If code compiles with only the root package on PYTHONPATH, it is
jurisdiction-agnostic by construction. Importing from a jurisdiction package
is a deliberate act; core should never require it.

Usage (in caller code like simulate.py):
    from countries.canada.adapter import CanadaAdapter
    adapter = CanadaAdapter(config)
    sim = FamilySimulation(config, adapter=adapter)
"""

from typing import Dict, List, Optional, Protocol, Tuple, Any, runtime_checkable
from dataclasses import dataclass, field


# ── Account Protocol ────────────────────────────────────────────────────────

@runtime_checkable
class AccountProtocol(Protocol):
    """Protocol for registered accounts (RRSP, TFSA, FHSA, RESP, NonReg).
    
    Core simulation uses these methods to contribute, grow, and query
    account state. Jurisdiction packages provide concrete implementations.
    """
    
    @property
    def balance(self) -> float:
        """Current account balance."""
        ...
    
    @balance.setter
    def balance(self, value: float) -> None:
        """Set current account balance."""
        ...
    
    @property
    def contribution_room(self) -> float:
        """Available contribution room."""
        ...
    
    @contribution_room.setter
    def contribution_room(self, value: float) -> None:
        """Set available contribution room."""
        ...
    
    def contribute(self, amount: float) -> float:
        """Contribute to the account. Returns actual amount contributed (clamped to room)."""
        ...
    
    def grow(self, rate: float) -> None:
        """Grow the account balance by the given rate."""
        ...


@runtime_checkable
class AccountWithAnnualRoom(AccountProtocol, Protocol):
    """Account that adds annual contribution room (RRSP, TFSA)."""
    
    annual_room: float
    annual_room_cap: float
    
    def add_annual_room(self, income: float = 0) -> None:
        """Add annual contribution room (may depend on income for RRSP)."""
        ...


@runtime_checkable
class CostBasisAccount(AccountProtocol, Protocol):
    """Account that tracks cost basis (ACB) for capital gains."""
    
    @property
    def cost_basis(self) -> float:
        """Adjusted cost basis."""
        ...
    
    @cost_basis.setter
    def cost_basis(self, value: float) -> None:
        """Set adjusted cost basis."""
        ...
    
    @property
    def unrealized_gains(self) -> float:
        """Unrealized capital gains (balance - cost_basis)."""
        ...


# ── HELOC Tracing Protocol ──────────────────────────────────────────────────

@dataclass
class HelocTracingEntry:
    """A record of a HELOC advance (investment-purpose tracking)."""
    amount: float = 0.0
    purpose: str = "unknown"  # "investment", "rrsp_contribution", "tfsa_contribution", "personal"
    description: str = ""


@runtime_checkable
class HelocTracingProtocol(Protocol):
    """Protocol for HELOC tracing (ITA §20(1)(c) deductibility tracking).
    
    Tracks which portions of HELOC advances are investment-purpose
    (deductible) vs. registered-account (not deductible) vs. personal.
    """
    
    def advance(self, amount: float, date: str, purpose: str, description: str = "") -> None:
        """Record a HELOC advance."""
        ...
    
    def deductible_interest(self, balance: float, rate: float) -> float:
        """Compute deductible interest from current balance and rate."""
        ...
    
    @property
    def advances(self) -> list:
        """List of advance records."""
        ...


# ── RESP Protocol ───────────────────────────────────────────────────────────

@runtime_checkable
class RESPChildProtocol(Protocol):
    """Protocol for a child in the RESP system."""
    
    @property
    def name(self) -> str:
        """Child's name (role-based, DP#4)."""
        ...
    
    @property
    def resp_balance(self) -> float:
        """Current RESP balance for this child."""
        ...
    
    @resp_balance.setter
    def resp_balance(self, value: float) -> None:
        """Set RESP balance."""
        ...
    
    def cesg_eligible(self, year: int) -> bool:
        """Whether this child is eligible for CESG in the given year."""
        ...


@runtime_checkable
class RESPCalculatorProtocol(Protocol):
    """Protocol for RESP grant calculation (CESG, QESI, CLB)."""
    
    def calculate_cesg(self, contribution: float, child: RESPChildProtocol,
                       year: int, family_income: float) -> Dict[str, float]:
        """Calculate CESG grant for a contribution.
        
        Returns dict with 'total_cesg' and other keys.
        """
        ...
    
    def calculate_qesi(self, contribution: float, child: RESPChildProtocol,
                       year: int, family_income: float) -> Dict[str, float]:
        """Calculate QESI grant for a contribution (Quebec-specific).
        
        Returns dict with 'total_qesi' and other keys.
        """
        ...


# ── Rate Model Protocol ─────────────────────────────────────────────────────

@runtime_checkable
class RatePathProtocol(Protocol):
    """Protocol for a mortgage rate path."""
    
    name: str
    rate_type: str
    
    def get_rate(self, year: int) -> float:
        """Get the mortgage rate for a given year index."""
        ...


@runtime_checkable
class HELOCPathProtocol(Protocol):
    """Protocol for a HELOC rate path."""
    
    def get_heloc_rate(self, year: int, rate_type: str = "variable") -> float:
        """Get the HELOC rate for a given year index."""
        ...


# ── Strategy Protocol ───────────────────────────────────────────────────────

@runtime_checkable
class StrategyDiscoveryProtocol(Protocol):
    """Protocol for strategy discovery (DP#6, DP#8).
    
    Discovers applicable strategies from current financial state,
    rather than hardcoding strategy names.
    """
    
    def discover_strategies(self, state: Any, config: Dict) -> Dict[str, Any]:
        """Discover applicable strategies given family state and config.
        
        Returns dict mapping strategy name to strategy object.
        """
        ...
    
    @property
    def default_strategy(self) -> Any:
        """Return the default (recommended) strategy."""
        ...


# ── QC Deduction Protocol ───────────────────────────────────────────────────

@dataclass
class QCDeductionResult:
    """Result of Quebec SM interest deduction computation."""
    qc_deductible: float = 0.0
    qc_carry_forward: float = 0.0
    readvance_interest: float = 0.0
    readvance_tax_savings: float = 0.0


@runtime_checkable
class QCDeductionProtocol(Protocol):
    """Protocol for Quebec interest deduction tracking.
    
    Tracks the carry-forward of unused Quebec SM deduction
    and computes the deductible amount each year.
    """
    
    @property
    def carry_forward(self) -> float:
        """Current carry-forward of unused deduction."""
        ...
    
    @carry_forward.setter
    def carry_forward(self, value: float) -> None:
        """Set carry-forward value."""
        ...


# ── Jurisdiction Adapter ────────────────────────────────────────────────────

@runtime_checkable
class JurisdictionAdapter(Protocol):
    """Top-level protocol for a jurisdiction adapter (DP#8, DP#25).
    
    The simulation engine receives a JurisdictionAdapter and uses it
    to create all jurisdiction-specific objects. This keeps core
    jurisdiction-agnostic: it never imports from countries/*.
    
    Each country package provides an adapter implementing this protocol.
    The adapter is created by the caller (e.g., simulate.py) and
    injected into FamilySimulation via the `adapter` parameter.
    """
    
    # ── Account factories ──
    
    def create_rrsp(self, contribution_room: float = 0) -> AccountProtocol:
        """Create an RRSP account."""
        ...
    
    def create_tfsa(self, contribution_room: float = 0) -> AccountProtocol:
        """Create a TFSA account."""
        ...
    
    def create_nonreg(self) -> CostBasisAccount:
        """Create a non-registered account."""
        ...
    
    def create_readvanceable_mortgage(self, heloc_rate: float = 0.05) -> Any:
        """Create a readvanceable mortgage tracker."""
        ...
    
    def create_fhsa(self, contribution_room: float = 0) -> AccountProtocol:
        """Create an FHSA account."""
        ...
    
    # ── RESP ──
    
    def create_resp_calculator(self) -> RESPCalculatorProtocol:
        """Create an RESP grant calculator."""
        ...
    
    def create_resp_child(self, name: str, birth_year: int,
                          is_quebec_resident: bool = True,
                          resp_balance: float = 0) -> RESPChildProtocol:
        """Create an RESP child record."""
        ...
    
    # ── HELOC tracing ──
    
    def create_heloc_tracing(self, name: str = "") -> HelocTracingProtocol:
        """Create a HELOC tracing tracker."""
        ...
    
    # ── QC deduction ──
    
    def create_qc_deduction(self) -> QCDeductionProtocol:
        """Create a Quebec deduction carry-forward tracker."""
        ...
    
    def compute_qc_sm_benefit(self, *, readvance_heloc_balance: float,
                               heloc_rate: float,
                               deductible_proportion: float,
                               nonreg_balance: float,
                               qc_carry_forward: float,
                               marginal_rate: float,
                               sim_year: int) -> QCDeductionResult:
        """Compute Quebec SM deduction benefit for one simulation year."""
        ...
    
    # ── Rate paths ──
    
    def build_rate_path(self, name: str, initial_rate: float,
                        term_years: int, rate_type: str = "fixed",
                        additional_rates: List[float] = None) -> RatePathProtocol:
        """Build a mortgage rate path."""
        ...
    
    def build_heloc_path(self, rate_path: RatePathProtocol,
                          heloc_rate: float = None) -> HELOCPathProtocol:
        """Build a HELOC rate path.

        ``heloc_rate``: the household's own declared HELOC rate, when
        known (#654). When provided, it wins outright and is never
        overridden by a value derived from ``rate_path`` (the mortgage's
        rate path) -- the two are different credit products. ``rate_path``
        is used only to derive a DP#13 placeholder rate when no HELOC rate
        was ever declared (``heloc_rate is None``).
        """
        ...
    
    # ── Amortization ──
    
    def amortization_schedule(self, *, principal: float,
                               rate_path: RatePathProtocol,
                               amortization_years: int,
                               projection_months: int,
                               readvance_smith: bool = False) -> List[Dict]:
        """Compute amortization schedule."""
        ...
    
    def annual_summary(self, schedule: List[Dict]) -> List[Dict]:
        """Summarize amortization schedule by year."""
        ...
    
    def monthly_payment(self, principal: float, annual_rate: float,
                        months: int) -> float:
        """Compute monthly mortgage payment."""
        ...
    
    # ── Strategy discovery ──
    
    def get_default_strategy(self) -> Any:
        """Get the default strategy for this jurisdiction."""
        ...
    
    def discover_strategies(self, state: Any, config: Dict) -> Dict[str, Any]:
        """Discover applicable strategies for this jurisdiction."""
        ...
    
    # ── HELOC tracing entries (for backward-compat sync) ──
    
    def create_advance_record(self, amount: float, date: str,
                              purpose: str, description: str = "") -> HelocTracingEntry:
        """Create a HELOC advance record."""
        ...
    
    # ── Tax provider ──
    
    def get_tax_provider(self) -> Any:
        """Get the jurisdiction's tax data provider for year-versioned brackets."""
        ...