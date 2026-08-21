#!/usr/bin/env python3
"""
Canada Jurisdiction Adapter — Implements JurisdictionAdapter for Canada.

This module bridges the gap between the jurisdiction-agnostic core
(simulation.py, simulation_state.py) and the Canada-specific modules
(countries/canada/*). Core depends only on the Protocol interfaces
in jurisdiction.py; this adapter provides concrete Canada implementations.

DP#8: Compose through data, not through inheritance.
DP#25: Core compiles without this module; importing it is a deliberate act.
DP#10: Core defines interfaces; jurisdiction packages provide implementations.

Usage (in simulate.py or caller code):
    from countries.canada.adapter import CanadaAdapter
    adapter = CanadaAdapter(config)
    sim = FamilySimulation(config, adapter=adapter)
"""

from typing import Dict, List, Any, Optional

from countries.canada.account_models import (
    RRSPAccount, TSFAccount, RESPAccount, NonRegAccount,
)
from countries.canada.fhsa import FHSAAccount
from countries.canada.resp_rules import RESPCalculator, RESPChild
from countries.canada.debt import (
    DebtPurpose, HELOCTracing, AdvanceRecord, DispositionRecord,
)
from countries.canada.provinces.quebec.quebec_deduction import (
    QuebecDeductionTracker, compute_sm_qc_benefit,
)
from countries.canada.rate_model import (
    RatePath, HELOCPath, ReadvanceableMortgage, build_rate_path, amortization_schedule,
    annual_summary, monthly_payment,
)
from countries.canada.strategies import (
    STRATEGY_READVANCE_PRIORITY, discover_strategies,
)
from countries.canada.market_rates import MarketRatesProvider

from jurisdiction import (
    QCDeductionResult,
    HelocTracingEntry,
)


# ── Purpose mapping ─────────────────────────────────────────────────────────
#
# Issue #656: this used to be a hand-maintained subset of DebtPurpose
# ("investment"/"rrsp_contribution"/"tfsa_contribution"/"personal") that
# could silently drift from the enum -- and DID, missing RENTAL_EXPENSE,
# RESP_CONTRIBUTION, and MIXED entirely. Built FROM DebtPurpose instead
# (keyed by each member's own .value) so it cannot drift: adding a
# DebtPurpose member automatically registers its mapping, and there is no
# separate string vocabulary ("rrsp_contribution" vs the enum's own "rrsp")
# to keep in sync by hand.
_PURPOSE_MAP: Dict[str, "DebtPurpose"] = {p.value: p for p in DebtPurpose}


class CanadaAdapter:
    """Jurisdiction adapter for Canadian tax and financial rules.
    
    Implements the JurisdictionAdapter protocol from jurisdiction.py,
    providing Canadian account models, RESP calculations, HELOC tracing,
    Quebec deductions, rate paths, and strategy discovery.
    """
    
    def __init__(self, config: Any = None):
        """Initialize the Canada adapter.
        
        Args:
            config: SimulationConfig (used for tax provider initialization)
        """
        self._config = config
        self._tax_provider = None
    
    # ── Account factories ──
    
    def create_rrsp(self, contribution_room: float = 0):
        """Create a Canadian RRSP account."""
        return RRSPAccount(contribution_room=contribution_room)
    
    def create_tfsa(self, contribution_room: float = 0):
        """Create a Canadian TFSA account."""
        return TSFAccount(contribution_room=contribution_room)
    
    def create_nonreg(self):
        """Create a Canadian non-registered account."""
        return NonRegAccount()
    
    def create_readvanceable_mortgage(self, heloc_rate: float = 0.05):
        """Create a Canadian readvanceable mortgage tracker."""
        return ReadvanceableMortgage(heloc_rate=heloc_rate)
    
    def create_fhsa(self, contribution_room: float = 0):
        """Create a Canadian FHSA account.
        
        Splits contribution_room into annual_room and carry_forward_room
        per CRA rules: annual room up to FHSA_ANNUAL_LIMIT, remainder is
        carry-forward (capped at FHSA_CARRY_FORWARD_MAX).
        """
        from countries.canada.fhsa import FHSA_ANNUAL_LIMIT, FHSA_CARRY_FORWARD_MAX, FHSA_LIFETIME_LIMIT
        annual = min(contribution_room, FHSA_ANNUAL_LIMIT)
        carry = min(max(0, contribution_room - annual), FHSA_CARRY_FORWARD_MAX)
        return FHSAAccount(annual_room=annual, carry_forward_room=carry,
                          lifetime_room=FHSA_LIFETIME_LIMIT)
    
    # ── RESP ──
    
    def create_resp_calculator(self):
        """Create a Canadian RESP calculator (CESG, QESI, CLB)."""
        return RESPCalculator()
    
    def create_resp_child(self, name: str, birth_year: int,
                          province: str = 'quebec',
                          resp_balance: float = 0):
        """Create a Canadian RESP child record.
        
        DP#16: Province is derived from config, not a boolean flag.
        When province='quebec' is present, QESI eligibility is auto-determined.
        """
        return RESPChild(
            name=name,
            birth_year=birth_year,
            province=province,
            is_quebec_resident=(province.lower() in ('quebec', 'qc')),
            resp_balance=resp_balance,
        )
    
    # ── HELOC tracing ──
    
    def create_heloc_tracing(self, name: str = ""):
        """Create a Canadian HELOC tracing tracker (ITA §20(1)(c))."""
        return HELOCTracing(name=name)
    
    # ── QC deduction ──
    
    def create_qc_deduction(self):
        """Create a Quebec deduction carry-forward tracker."""
        return QuebecDeductionTracker()
    
    def compute_qc_sm_benefit(self, *, readvance_heloc_balance: float,
                               heloc_rate: float,
                               deductible_proportion: float,
                               nonreg_balance: float,
                               qc_carry_forward: float,
                               marginal_rate: float,
                               sim_year: int) -> QCDeductionResult:
        """Compute Quebec SM deduction benefit for one simulation year."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=readvance_heloc_balance,
            heloc_rate=heloc_rate,
            deductible_proportion=deductible_proportion,
            nonreg_balance=nonreg_balance,
            qc_carry_forward=qc_carry_forward,
            marginal_rate=marginal_rate,
            sim_year=sim_year,
        )
        return QCDeductionResult(
            qc_deductible=result['qc_deductible'],
            qc_carry_forward=result['qc_carry_forward'],
            readvance_interest=result['readvance_interest'],
            readvance_tax_savings=result['readvance_tax_savings'],
        )
    
    # ── Rate paths ──
    
    def build_rate_path(self, name: str, initial_rate: float,
                        term_years: int, rate_type: str = "fixed",
                        additional_rates: List[float] = None):
        """Build a Canadian mortgage rate path."""
        if additional_rates is None:
            additional_rates = []
        return build_rate_path(name, initial_rate, term_years, rate_type, additional_rates)
    
    def build_heloc_path(self, rate_path, heloc_rate: Optional[float] = None):
        """Build a Canadian HELOC rate path.

        Issue #654: ``heloc_rate`` is the household's OWN declared HELOC
        rate (``SimulationConfig.heloc_rate``, mapped from
        ``liabilities[kind=heloc].rate``). When supplied, it is the rate
        the returned path reports for every year -- it does not derive a
        HELOC rate from the mortgage's rate path at all. A cheap legacy
        fixed mortgage alongside a prime-linked revolving HELOC is the
        ordinary case, not an edge case: the two rates are different facts
        about different credit products and must not be conflated (DP#32).

        ``rate_path`` (the mortgage's rate path) is used only as the
        DP#13 placeholder-derivation basis when ``heloc_rate`` is None --
        i.e. when no HELOC rate was ever declared at all, not as a
        fallback that overrides one that was.
        """
        return HELOCPath(
            name=f"HELOC for {rate_path.name}",
            mortgage_rate_path=rate_path,
            fixed_rate=heloc_rate,
        )
    
    # ── Amortization ──
    
    def amortization_schedule(self, *, principal: float,
                               rate_path,
                               amortization_years: int,
                               projection_months: int,
                               readvance_smith: bool = False):
        """Compute Canadian amortization schedule."""
        return amortization_schedule(
            principal=principal,
            rate_path=rate_path,
            amortization_years=amortization_years,
            projection_months=projection_months,
            readvance_smith=readvance_smith,
        )
    
    def annual_summary(self, schedule: List[Dict]):
        """Summarize Canadian amortization schedule by year."""
        return annual_summary(schedule)
    
    def monthly_payment(self, principal: float, annual_rate: float,
                        months: int) -> float:
        """Compute monthly mortgage payment."""
        return monthly_payment(principal, annual_rate, months)
    
    # ── Strategy discovery ──
    
    def get_default_strategy(self):
        """Get the default Canadian strategy (Smith Manoeuvre priority)."""
        return STRATEGY_READVANCE_PRIORITY
    
    def discover_strategies(self, state, config: Dict,
                            return_model=None,
                            investment_return: float = None,
                            heloc_rate: float = None) -> Dict[str, Any]:
        """Discover applicable Canadian strategies from family state.

        Forwards financial params to strategies.discover_strategies.
        Either return_model or investment_return must be provided,
        and heloc_rate must be provided (issue #23).
        """
        return discover_strategies(
            state, config,
            return_model=return_model,
            investment_return=investment_return,
            heloc_rate=heloc_rate,
        )
    
    # ── HELOC tracing entries (for backward-compat sync) ──
    
    def create_advance_record(self, amount: float, date: str,
                              purpose: str, description: str = "") -> HelocTracingEntry:
        """Create a HELOC advance record.
        
        Maps purpose strings to Canada-specific DebtPurpose enum values.
        """
        return HelocTracingEntry(
            amount=amount,
            purpose=purpose,
            description=description,
        )
    
    def create_debt_purpose(self, purpose: str):
        """Map a purpose string to a Canada DebtPurpose enum value.

        Issue #656: an unrecognised purpose string used to silently default
        to ``DebtPurpose.INVESTMENT`` -- the one value whose interest is
        tax-deductible under ITA s.20(1)(c). A typo, a renamed key, or a new
        purpose added upstream and never registered here would silently
        convert non-deductible borrowing into deductible borrowing,
        manufacturing a tax deduction ITA s.18(11) expressly prohibits for
        RRSP/TFSA-purpose debt. There is no defensible default: the "safe"
        direction (``PERSONAL``) would just as silently destroy a
        legitimate deduction. Absence must fail loudly instead (DP#32) --
        direct indexing lets ``KeyError`` propagate, naming the bad value
        and the ``DebtPurpose`` member it belongs to.
        """
        try:
            return _PURPOSE_MAP[purpose]
        except KeyError:
            raise KeyError(
                f"Unknown debt purpose {purpose!r} -- not a DebtPurpose "
                f"value. Valid purposes: {sorted(_PURPOSE_MAP)}. Refusing "
                f"to guess (a wrong guess here silently invents or "
                f"destroys a tax deduction, issue #656)."
            ) from None
    
    # ── Tax provider ──
    
    def get_tax_provider(self):
        """Get the tax data provider for year-versioned brackets and limits."""
        if self._tax_provider is None:
            from tax_data import TaxDataProvider
            self._tax_provider = TaxDataProvider()
        return self._tax_provider