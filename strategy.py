#!/usr/bin/env python3
"""
Strategy Module — Composable Allocation Strategies

Defines how savings are distributed across accounts. Each strategy is a
dataclass that can be mixed, matched, or customized. Strategies can be
parameterized for simple (flat %) or advanced (bracket-aware) allocation.

The allocation engine takes a strategy config + family state and produces
contribution decisions per account per year.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RRSP/TFSA/FHSA entries
    RRSP contribution limits (18% of earned income, max dollar limit per year):
        https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html
    TFSA annual room ($7,000 for 2024-2026):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html
    FHSA annual limit ($8,000) and lifetime limit ($40,000):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html
    HBP withdrawal limit ($60,000):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan.html + family state and produces
contribution decisions per account per year.

Usage:
    from strategy import AllocationStrategy, StrategyEngine, list_strategies
    # Country strategies self-register at package-import time (DP#25, #284);
    # discover them through the registry rather than importing a country module.
    strategy = list_strategies()["readvance_priority"]
    engine = StrategyEngine(strategy=strategy)
    contributions = engine.allocate(annual_savings=state.annual_savings, state=family_state)
"""


from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


def _cesg_contribution_match_max(year: int = 2026) -> float:
    """Max annual RESP contribution per child that earns CESG matching.

    DP#8/DP#10 (#241): this is a Canadian CESG Act figure ($2,500 since 2005),
    owned by countries.canada.resp_rules — not a literal in core allocation
    logic. Imported lazily to keep strategy.py jurisdiction-agnostic and to
    avoid an import-time cycle.
    """
    from countries.canada.resp_rules import get_cesg_contribution_max
    return get_cesg_contribution_max(year)


def _fhsa_annual_limit() -> float:
    """Annual FHSA contribution limit, owned by countries.canada.fhsa (#241)."""
    from countries.canada.fhsa import FHSA_ANNUAL_LIMIT
    return FHSA_ANNUAL_LIMIT


def _resp_match_max(state: 'FamilyState') -> float:
    """Per-child CESG-matched contribution cap for an allocation decision.

    Prefers the value carried on the state (config/jurisdiction-supplied);
    otherwise reads the Canada package figure. Never a bare literal in logic.
    """
    if getattr(state, 'resp_contribution_match_max', 0) > 0:
        return state.resp_contribution_match_max
    return _cesg_contribution_match_max()


# =============================================================================
# Strategy Types
# =============================================================================

class StrategyType(Enum):
    """Strategy categories discovered from family/financial conditions."""
    BALANCED = "balanced"
    RRSP_MAX = "rrsp_max"
    CUSTOM = "custom"
    # Discovery-based: these are added when conditions hold, not pre-defined
    READVANCE_PRIORITY = "readvance_priority"  # Discovered when readvanceable + deductible + profitable
    NO_READVANCE = "no_readvance"  # Baseline when readvancing not applicable


@dataclass
class AllocationResult:
    """Result of a single year's allocation across accounts.
    
    All amounts in dollars. This is a pure data object — no logic.
    """
    primary_rrsp: float = 0.0
    spousal_rrsp: float = 0.0
    spouse_rrsp: float = 0.0
    primary_tfsa: float = 0.0
    spouse_tfsa: float = 0.0
    fhsa: float = 0.0
    resp: float = 0.0
    non_reg: float = 0.0
    unused: float = 0.0  # Any savings not allocated
    
    @property
    def total_allocated(self) -> float:
        return (self.primary_rrsp + self.spousal_rrsp + self.spouse_rrsp +
                self.primary_tfsa + self.spouse_tfsa + self.fhsa + self.resp + self.non_reg)
    
    def as_dict(self) -> Dict[str, float]:
        return {
            'primary_rrsp': self.primary_rrsp,
            'spousal_rrsp': self.spousal_rrsp,
            'spouse_rrsp': self.spouse_rrsp,
            'primary_tfsa': self.primary_tfsa,
            'spouse_tfsa': self.spouse_tfsa,
            'fhsa': self.fhsa,
            'resp': self.resp,
            'non_reg': self.non_reg,
            'unused': self.unused,
        }


@dataclass
class ChildState:
    """A single child's OWN savings and contribution room (issue #812).

    All room values are AVAILABLE room, not totals. This is the child-side
    analogue of FamilyState: what StrategyEngine.allocate_child needs to route
    a child's OWN incremental savings into the child's OWN accounts.

    #701: a child is NOT taxed as an individual. So these accounts carry no
    household deduction — a child's RRSP/FHSA contribution reduces ~no tax at
    low income (the unused deduction room carries forward), while TFSA/FHSA
    growth is tax-free regardless. allocate_child therefore routes DOLLARS by
    room only; it invents no tax the child does not pay, and it never claims
    the household's marginal-rate deduction on a child's contribution.
    """
    savings: float = 0.0                # the child's OWN incremental savings this year
    tfsa_room: float = 0.0
    fhsa_room: float = 0.0              # available annual FHSA room
    fhsa_lifetime_remaining: float = 0.0
    rrsp_room: float = 0.0
    name: str = 'child'


@dataclass
class ChildAllocationResult:
    """Where a child's OWN savings landed for one year (issue #812).

    Pure data object — no logic, no tax. non_reg is the residual sink.
    """
    tfsa: float = 0.0
    fhsa: float = 0.0
    rrsp: float = 0.0
    non_reg: float = 0.0
    unused: float = 0.0

    @property
    def total_allocated(self) -> float:
        return self.tfsa + self.fhsa + self.rrsp + self.non_reg

    def as_dict(self) -> Dict[str, float]:
        return {
            'tfsa': self.tfsa,
            'fhsa': self.fhsa,
            'rrsp': self.rrsp,
            'non_reg': self.non_reg,
            'unused': self.unused,
        }


@dataclass
class AllocationStrategy:
    """Defines how to allocate annual savings across accounts.
    
    Composable: can be simple (flat percentages) or advanced
    (bracket-aware, SM-focused, deduct-later).
    
    Attributes:
        name: Human-readable strategy name
        strategy_type: Category for grouping
        rrsp_pct: Percentage of savings to personal RRSP
        spousal_rrsp_pct: Percentage to spousal RRSP
        tfsa_pct: Percentage to TFSA
        fhsa_pct: Percentage to FHSA (first home savings account)
        resp_pct: Percentage to RESP
        non_reg_pct: Percentage to non-registered (remainder goes here)
        child_tfsa_pct: A child's OWN savings target share to their OWN TFSA (#812)
        child_fhsa_pct: A child's OWN savings target share to their OWN FHSA (#812)
        child_rrsp_pct: A child's OWN savings target share to their OWN RRSP (#812)
        child_non_reg_pct: A child's OWN savings target share to their OWN non-reg (#812)
        prioritize_readvanceable: Maximize non-reg for deductible investment (readvanceable mortgage)
        deduct_later: Spread RRSP deduction over time
        spousal_splitting: Use spousal RRSP if bracket gap > threshold
        min_bracket_gap: Minimum bracket gap for spousal RRSP
        bracket_target: Income target for deduct-later strategy
    """
    name: str = "Custom"
    strategy_type: StrategyType = StrategyType.CUSTOM
    rrsp_pct: float = 0.30
    spousal_rrsp_pct: float = 0.10
    tfsa_pct: float = 0.30
    fhsa_pct: float = 0.0  # Default 0; enable when first-home purchase is planned
    resp_pct: float = 0.07
    non_reg_pct: float = 0.23
    # Issue #812 (#701 follow-up): a child's OWN incremental savings route to
    # the CHILD's own accounts, not the household pot. These four are TARGET
    # shares of the child's own savings; StrategyEngine.allocate_child caps each
    # by the child's own room and spills the residual by a #701 priority
    # waterfall (TFSA -> FHSA -> RRSP -> non-reg). Defaults are 0.0 so a
    # household that declares no child allocation is inert: allocate_child then
    # falls back to the pure room-priority waterfall, and a household with no
    # child savings routes nothing at all (DP#32: absence, not a defaulted split).
    child_tfsa_pct: float = 0.0
    child_fhsa_pct: float = 0.0
    child_rrsp_pct: float = 0.0
    child_non_reg_pct: float = 0.0
    prioritize_readvanceable: bool = False
    deduct_later: bool = False
    spousal_splitting: bool = True
    min_bracket_gap: float = 0.10
    bracket_target: float = 117045.0
    
    @property
    def total_pct(self) -> float:
        """Verify percentages sum to ~1.0."""
        return (self.rrsp_pct + self.spousal_rrsp_pct + self.tfsa_pct +
                self.fhsa_pct + self.resp_pct + self.non_reg_pct)
    
    def validate(self) -> List[str]:
        """Check strategy configuration for issues. Returns list of warnings."""
        warnings = []
        total = self.total_pct
        if abs(total - 1.0) > 0.05:
            warnings.append(f"Percentages sum to {total:.1%}, not 100%")
        if self.spousal_rrsp_pct > 0 and not self.spousal_splitting:
            warnings.append("Spousal RRSP allocated but splitting disabled")
        if self.prioritize_readvanceable and self.non_reg_pct < 0.4:
            warnings.append("SM priority enabled but non-reg < 40%")
        # Issue #812: child allocation targets are shares of the CHILD's own
        # savings; a declared set summing above 1.0 over-targets that pool.
        child_total = (self.child_tfsa_pct + self.child_fhsa_pct +
                       self.child_rrsp_pct + self.child_non_reg_pct)
        if child_total > 1.0 + 0.05:
            warnings.append(f"Child allocation targets sum to {child_total:.1%}, over 100%")
        return warnings

    @classmethod
    def from_dict(cls, data: dict) -> 'AllocationStrategy':
        """Create an AllocationStrategy from config dict (DP#2/DP#6).

        DP#2: Configuration belongs in input, not in code. Strategy
        allocation percentages should come from user config, not hardcoded.
        DP#6: Strategies are discovered from rules, not named by convention.

        Args:
            data: Dict with strategy fields. Missing fields use defaults.
        """
        strategy_type = data.get('strategy_type', 'custom')
        if isinstance(strategy_type, str):
            strategy_type = StrategyType(strategy_type)
        return cls(
            name=data.get('name', 'Custom'),
            strategy_type=strategy_type,
            rrsp_pct=data.get('rrsp_pct', 0.30),
            spousal_rrsp_pct=data.get('spousal_rrsp_pct', 0.10),
            tfsa_pct=data.get('tfsa_pct', 0.30),
            fhsa_pct=data.get('fhsa_pct', 0.0),
            resp_pct=data.get('resp_pct', 0.07),
            non_reg_pct=data.get('non_reg_pct', 0.23),
            child_tfsa_pct=data.get('child_tfsa_pct', 0.0),
            child_fhsa_pct=data.get('child_fhsa_pct', 0.0),
            child_rrsp_pct=data.get('child_rrsp_pct', 0.0),
            child_non_reg_pct=data.get('child_non_reg_pct', 0.0),
            prioritize_readvanceable=data.get('prioritize_readvanceable', False),
            deduct_later=data.get('deduct_later', False),
            spousal_splitting=data.get('spousal_splitting', True),
            min_bracket_gap=data.get('min_bracket_gap', 0.10),
            bracket_target=data.get('bracket_target', 117045.0),
        )

    def to_dict(self) -> dict:
        """Export to dict matching input.json schema. DP#24."""
        return {
            'name': self.name,
            'strategy_type': self.strategy_type.value,
            'rrsp_pct': self.rrsp_pct,
            'spousal_rrsp_pct': self.spousal_rrsp_pct,
            'tfsa_pct': self.tfsa_pct,
            'fhsa_pct': self.fhsa_pct,
            'resp_pct': self.resp_pct,
            'non_reg_pct': self.non_reg_pct,
            'child_tfsa_pct': self.child_tfsa_pct,
            'child_fhsa_pct': self.child_fhsa_pct,
            'child_rrsp_pct': self.child_rrsp_pct,
            'child_non_reg_pct': self.child_non_reg_pct,
            'prioritize_readvanceable': self.prioritize_readvanceable,
            'deduct_later': self.deduct_later,
            'spousal_splitting': self.spousal_splitting,
            'min_bracket_gap': self.min_bracket_gap,
            'bracket_target': self.bracket_target,
        }


@dataclass
class FamilyState:
    """Snapshot of family financial state for allocation decisions.
    
    This is what the allocation engine needs to make decisions.
    All room values are available contribution room, not totals.
    """
    # Incomes (DP#13: defaults are fallbacks, not opinions)
    primary_income: float = 0.0
    spouse_income: float = 0.0
    primary_marginal_rate: float = 0.0
    spouse_marginal_rate: float = 0.0
    
    # Available contribution room
    primary_rrsp_room: float = 0.0
    spouse_rrsp_room: float = 0.0
    primary_tfsa_room: float = 0.0   # DP#13: personal data — set from input
    spouse_tfsa_room: float = 0.0    # DP#13: personal data — set from input

    # FHSA (first-time home buyers)
    fhsa_room: float = 0.0  # DP#13: set from TaxDataProvider; 2023-2025: $8,000/year
    fhsa_lifetime_remaining: float = 0.0  # DP#13: set from TaxDataProvider; 2023-2025: $40,000 lifetime

    # RESP
    resp_eligible_children: int = 1  # Number of children still eligible for CESG/QESI
    resp_annual_match_cap: float = 0.0  # DP#13: set from resp_rules; 2026 Quebec: $750
    # DP#8/DP#10 (#241): max annual contribution per child on which CESG matches.
    # This is a Canadian CESG Act figure owned by countries.canada.resp_rules
    # (get_cesg_contribution_max). 0 means "use the Canada package value" — the
    # literal is not hardcoded here, see _cesg_contribution_match_max().
    resp_contribution_match_max: float = 0.0
    
    # Family totals
    annual_savings: float = 0.0   # DP#13: personal data — set from input
    bracket_gap: float = 0.0      # DP#13: computed from marginal rates, not hardcoded


# =============================================================================
# Strategy Registry (populated by country modules via register())
# =============================================================================

_STRATEGY_REGISTRY: Dict[str, AllocationStrategy] = {}




# =============================================================================
# Allocation Engine
# =============================================================================


# =============================================================================
# Allocation Engine
# =============================================================================

class StrategyEngine:
    """Allocates annual savings across accounts based on a strategy.
    
    This is a pure function engine — given a strategy config and family state,
    it produces an AllocationResult with no side effects.
    
    Handles:
    - Contribution room limits (can't exceed available room)
    - Spousal RRSP only if bracket gap exceeds threshold
    - RESP eligibility (only eligible children get matching)
    - Deduct-later RRSP strategy (limit deduction to bracket target)
    - Readvanceable mortgage priority (maximize non-reg when enabled)
    """
    
    def __init__(self, strategy: AllocationStrategy):
        self.strategy = strategy
    
    def allocate(self, state: FamilyState,
                 initial_investment: Dict[str, float] = None) -> AllocationResult:
        """Allocate savings across accounts for one year.
        
        Args:
            state: Current family financial state
            initial_investment: Optional lump-sum investment (refinance cash)
        
        Returns:
            AllocationResult with contribution amounts per account
        """
        s = self.strategy
        savings = state.annual_savings
        result = AllocationResult()
        remaining = savings
        
        # Step 1: RESP (only for eligible children)
        resp_amount = min(
            s.resp_pct * savings,
            s.resp_pct * state.annual_savings,  # Cap at strategy %
            state.resp_annual_match_cap * state.resp_eligible_children,
            _resp_match_max(state) * state.resp_eligible_children,  # CESG-matched contribution per child
        )
        resp_amount = max(0, min(resp_amount, remaining))
        result.resp = resp_amount
        remaining -= resp_amount
        
        # Step 2: Spousal RRSP (only if bracket gap > threshold)
        # Spousal RRSP contributions use the primary earner's deduction room
        spousal_amount = 0.0
        if s.spousal_splitting and state.bracket_gap > s.min_bracket_gap:
            spousal_amount = min(
                s.spousal_rrsp_pct * savings,
                state.primary_rrsp_room,
                remaining * 0.5,
            )
            spousal_amount = max(0, min(spousal_amount, state.primary_rrsp_room, remaining))
        
        result.spousal_rrsp = spousal_amount
        remaining -= spousal_amount
        primary_room_for_own = max(0, state.primary_rrsp_room - spousal_amount)
        
        # Step 3: Personal RRSP
        # When deduct_later: contribute FULL amount (same as deduct-now),
        # but tax savings are computed from the DEDUCTION claimed, not the contribution.
        # The carry-forward is tracked in the simulation (rrsp_undeducted pool).
        rrsp_amount = min(
            s.rrsp_pct * savings,
            primary_room_for_own,
            remaining,
        )
        
        rrsp_amount = max(0, rrsp_amount)
        result.primary_rrsp = rrsp_amount
        remaining -= rrsp_amount
        
        # Step 4: FHSA (first home savings account — time-limited, high-priority)
        # FHSA comes before TFSA because it has a contribution deadline
        # and provides tax-deductible contributions like RRSP.
        if s.fhsa_pct > 0 and state.fhsa_room > 0 and state.fhsa_lifetime_remaining > 0:
            fhsa_amount = min(
                s.fhsa_pct * savings,
                state.fhsa_room,
                state.fhsa_lifetime_remaining,
                remaining,
            )
            fhsa_amount = max(0, fhsa_amount)
        else:
            fhsa_amount = 0.0
        result.fhsa = fhsa_amount
        remaining -= fhsa_amount
        
        # Step 5: Spouse's personal RRSP (uses spouse's own room)
        a_rrsp = min(
            remaining * 0.3,  # Small portion
            state.spouse_rrsp_room,
            remaining,
        )
        a_rrsp = max(0, a_rrsp)
        result.spouse_rrsp = a_rrsp
        remaining -= a_rrsp
        
        # Step 6: TFSA (Primary and Spouse) — honours the declared tfsa_pct
        # (DP#2/DP#13/DP#32, issue #751). The previous `remaining * 0.5` hardcoded
        # a 50% split of whatever was left and ignored `s.tfsa_pct` entirely, so
        # a household that declared `tfsa_pct: 0.20` could see 52% of savings land
        # in TFSA. The declared `tfsa_pct` is the target share of *savings* for
        # TFSA; it is split evenly between the two spouses (no per-spouse
        # percentage is declared in the contract), then capped by each spouse's
        # contribution room and by what remains. tfsa_pct is never defaulted
        # inside allocate(): AllocationStrategy carries it, either from config or
        # its own DP#13 fallback for absent *input* — allocate() never silently
        # substitutes 0.5 (DP#32: zero is a value, absence must not default).
        # Any TFSA target that cannot be absorbed by a spouse's room spills to
        # non-reg (Step 7), the declared residual.
        tfsa_target = s.tfsa_pct * savings
        n_tfsa = min(
            tfsa_target * 0.5,
            state.primary_tfsa_room,
            remaining,
        )
        n_tfsa = max(0, n_tfsa)
        result.primary_tfsa = n_tfsa
        remaining -= n_tfsa
        
        a_tfsa = min(
            tfsa_target * 0.5,
            state.spouse_tfsa_room,
            remaining,
        )
        a_tfsa = max(0, a_tfsa)
        result.spouse_tfsa = a_tfsa
        remaining -= a_tfsa
        
        # (FHSA is allocated at Step 4, before TFSA)
        
        # Step 7: Non-registered (remainder) — honours non_reg_pct as the
        # declared residual. The six contract percentages sum to ~1.0
        # (`total_pct`/`validate`); with the five registered targets above
        # (resp, spousal_rrsp, rrsp, fhsa, tfsa) honoured and contribution room
        # sufficient, `remaining` here equals `non_reg_pct * savings` by
        # construction. non_reg also absorbs spill from any registered account
        # whose room is insufficient — which is the field's stated purpose
        # ("remainder goes here"). It is therefore not a silent no-op: it tracks
        # the declared split and the room-constrained overflow (issue #751).
        result.non_reg = max(0, remaining)
        remaining = 0
        
        # Step 8: Apply initial investment (refinance lump sum)
        if initial_investment:
            for key, value in initial_investment.items():
                if hasattr(result, key):
                    setattr(result, key, getattr(result, key) + value)
                elif key == 'tfsa':
                    result.primary_tfsa += value
                elif key in ('non_reg', 'non-reg'):
                    result.non_reg += value
        
        result.unused = max(0, savings - result.total_allocated)
        return result

    def allocate_child(self, child: ChildState) -> ChildAllocationResult:
        """Route a CHILD's OWN savings into the CHILD's OWN accounts (issue #812).

        Pure function: given the child's own savings + own room + the strategy's
        child_*_pct targets, produce a ChildAllocationResult. No side effects,
        no hidden state (DP#3).

        This answers the question the engine previously could not: "where should
        a child's incremental income go — TFSA / FHSA / RRSP / non-reg?" It is
        the child-side analogue of allocate(), deliberately kept separate so a
        child's dollars never mix into the household deduction math.

        #701 (children are not taxed as individuals): the routing is by ROOM,
        not by tax. A child's RRSP/FHSA contribution reduces ~no tax at low
        income and the unused *deduction* room carries forward — so this method
        NEVER invents a deduction for the child. The DEFAULT priority (when no
        child_*_pct is declared) fills the tax-free room first — TFSA, then FHSA
        — before RRSP (deferral with no upfront benefit) and finally non-reg
        (no room cap). That default is the #701-correct answer; the declared
        child_*_pct let a sweep override it to compare the alternatives.

        DP#32: savings <= 0 returns an all-zero result — a child with no savings
        genuinely routes nothing; that is absence, not a silently-defaulted 0.
        """
        s = self.strategy
        savings = child.savings
        result = ChildAllocationResult()
        if savings <= 0:
            return result

        # Available room per account. non-reg has no contribution limit, so it
        # is the residual sink (infinite headroom).
        fhsa_cap = max(0.0, min(child.fhsa_room, child.fhsa_lifetime_remaining))
        caps = {
            'tfsa': max(0.0, child.tfsa_room),
            'fhsa': fhsa_cap,
            'rrsp': max(0.0, child.rrsp_room),
            'non_reg': float('inf'),
        }
        pcts = {
            'tfsa': s.child_tfsa_pct,
            'fhsa': s.child_fhsa_pct,
            'rrsp': s.child_rrsp_pct,
            'non_reg': s.child_non_reg_pct,
        }
        # #701 priority waterfall: tax-free room first, non-reg last.
        priority = ('tfsa', 'fhsa', 'rrsp', 'non_reg')

        remaining = savings
        # Step 1: honour each declared target, capped by room and by what's left.
        for name in priority:
            amount = max(0.0, min(pcts[name] * savings, caps[name], remaining))
            setattr(result, name, amount)
            remaining -= amount
        # Step 2: spill the residual (untargeted savings, or a target that its
        # room could not absorb) down the #701 priority order into any headroom.
        for name in priority:
            if remaining <= 0:
                break
            headroom = caps[name] - getattr(result, name)
            amount = max(0.0, min(headroom, remaining))
            setattr(result, name, getattr(result, name) + amount)
            remaining -= amount

        # non_reg's infinite headroom absorbs anything left; unused stays 0
        # unless a future variant caps non_reg (kept explicit, not assumed).
        result.unused = max(0.0, remaining)
        return result

    def fill_room(self, lump_sum: float, state: FamilyState,
                 deductible_non_reg_first: Optional[float] = None) -> AllocationResult:
        """Allocate a lump sum to fill registered accounts in priority order.
        
        This is the "fill room first" waterfall that replaces the old
        refinance_optimizer's DynamicAllocator. Priorities:
        
        1. Primary RRSP (fill room first — deduct-later: only to bracket target)
        2. Spousal RRSP (if bracket gap > threshold)
        3. Spouse personal RRSP (remaining room)
        4. Primary TFSA (fill room)
        5. Spouse TFSA (fill room)
        6. RESP (only to get government matching)
        7. Non-reg (remainder — only if SM or high risk tolerance)
        
        Issue #792: when ``deductible_non_reg_first`` is not None, the
        household has DECLARED how much of the advance to route into the
        DEDUCTIBLE non-reg account first (s.20(1)(c) interest tracing is
        established only when borrowed money is deployed into income-producing
        non-reg at the refinance; borrowed money into RRSP/TFSA is
        non-deductible forever, s.18(11)). That amount is front-loaded into
        non-reg BEFORE the registered waterfall runs, and registered is then
        filled from the remainder (backfilled from ongoing income in later
        years via ``allocate``). ``None`` means "no declared split" -- the
        engine keeps today's internal optimization (fill registered first,
        non-reg gets the remainder). A declared 0 is a real choice (route
        nothing to deductible non-reg first) and is honoured as 0, distinct
        from absence -- both are the household's call, not the engine's.
        Args:
            lump_sum: Total amount available to invest (from margin + cash-out)
            state: Current family financial state
            deductible_non_reg_first: Issue #792 -- dollar amount of the
                advance to route to deductible non-reg BEFORE filling
                registered room. None = today's behaviour (registered first).
        
        Returns:
            AllocationResult with contributions per account
        """
        s = self.strategy
        result = AllocationResult()
        remaining = lump_sum
        
        # Issue #792: front-load the declared deductible non-reg amount BEFORE
        # the registered waterfall. The borrowed money traced to income-
        # producing non-reg establishes s.20(1)(c) deductibility at the
        # refinance (irreversibly), so the household -- not the engine --
        # decides this split. Capped at the lump sum (cannot route more to
        # non-reg than the advance provides) and never negative. A declared 0
        # routes nothing here (numerically identical to today's behaviour, but
        # a real declared choice); None skips this block entirely so today's
        # internal optimization is byte-for-byte unchanged when no split is
        # declared (DP#32: the absent path is the preserved path).
        if deductible_non_reg_first is not None:
            front_load = max(0.0, min(deductible_non_reg_first, remaining))
            result.non_reg = front_load
            remaining -= front_load

        # Step 1 + 2: Primary + Spousal RRSP (share the primary earner's room)
        # Split primary's room between personal and spousal using strategy ratios
        total_primary_rrsp_ratio = s.rrsp_pct + s.spousal_rrsp_pct
        if s.spousal_splitting and state.bracket_gap > s.min_bracket_gap and total_primary_rrsp_ratio > 0:
            primary_share = s.rrsp_pct / total_primary_rrsp_ratio
            spousal_share = s.spousal_rrsp_pct / total_primary_rrsp_ratio
            primary_drain = min(state.primary_rrsp_room * primary_share, remaining)
            spousal_drain = min(state.primary_rrsp_room * spousal_share, remaining - primary_drain)
            # Also check total doesn't exceed primary room
            total_drain = primary_drain + spousal_drain
            if total_drain > state.primary_rrsp_room:
                scale = state.primary_rrsp_room / total_drain
                primary_drain *= scale
                spousal_drain *= scale
        else:
            primary_drain = min(state.primary_rrsp_room, remaining)
            spousal_drain = 0
        
        result.primary_rrsp = primary_drain
        remaining -= primary_drain
        
        if spousal_drain > 0:
            result.spousal_rrsp = spousal_drain
            remaining -= spousal_drain
        
        # Step 3: Spouse personal RRSP (uses spouse's own room)
        a_rrsp = min(state.spouse_rrsp_room, remaining)
        result.spouse_rrsp = a_rrsp
        remaining -= a_rrsp
        
        # Step 4: Primary TFSA
        n_tfsa = min(state.primary_tfsa_room, remaining)
        result.primary_tfsa = n_tfsa
        remaining -= n_tfsa
        
        # Step 5: Spouse TFSA
        a_tfsa = min(state.spouse_tfsa_room, remaining)
        result.spouse_tfsa = a_tfsa
        remaining -= a_tfsa
        
        # Step 6: RESP (just enough for matching)
        resp_match = _resp_match_max(state) * state.resp_eligible_children  # CESG-matched contribution per child
        resp_amount = min(resp_match, remaining)
        result.resp = resp_amount
        remaining -= resp_amount
        
        # Step 6.5: FHSA (fill room — only if home purchase planned)
        if state.fhsa_room > 0 and state.fhsa_lifetime_remaining > 0:
            fhsa_amount = min(state.fhsa_room, state.fhsa_lifetime_remaining, remaining)
            result.fhsa = fhsa_amount
            remaining -= fhsa_amount
        
        # Step 7: Non-reg (remainder). Added to any issue #792 front-load
        # above (result.non_reg already holds the declared deductible amount;
        # the registered waterfall's remainder piles on top of it).
        result.non_reg += remaining
        remaining = 0
        
        result.unused = 0
        return result
    
    def allocate_year_by_year(self, state: FamilyState,
                               years: int = 10,
                               salary_growth: float = 0.02,
                               rrsp_room_growth: float = 0.18,  # DP#2: statutory rate (18% per ITA s.146(1)), not a personal default
                               tfsa_room_growth: float = 0.02,
                               rrsp_annual_max: float = 0,
                               tfsa_annual_limit: float = 0,
                               fhsa_annual_limit: float = 0) -> List[AllocationResult]:
        """Allocate savings for multiple years with compounding room.
        
        Args:
            state: Initial family state
            years: Number of years to project
            salary_growth: Annual salary growth rate
            rrsp_room_growth: Annual RRSP room growth (% of income)
            tfsa_room_growth: Annual TFSA room growth rate
        
        Returns:
            List of AllocationResult, one per year
        """
        results = []
        current_state = FamilyState(
            primary_income=state.primary_income,
            spouse_income=state.spouse_income,
            primary_marginal_rate=state.primary_marginal_rate,
            spouse_marginal_rate=state.spouse_marginal_rate,
            primary_rrsp_room=state.primary_rrsp_room,
            spouse_rrsp_room=state.spouse_rrsp_room,
            primary_tfsa_room=state.primary_tfsa_room,
            spouse_tfsa_room=state.spouse_tfsa_room,
            resp_eligible_children=state.resp_eligible_children,
            resp_annual_match_cap=state.resp_annual_match_cap,
            resp_contribution_match_max=state.resp_contribution_match_max,
            annual_savings=state.annual_savings,
            bracket_gap=state.bracket_gap,
        )
        
        for year in range(years):
            # Grow income
            current_state.primary_income *= (1 + salary_growth)
            current_state.spouse_income *= (1 + salary_growth)
            
            # Grow contribution room (DP#12: limits come from data, not hardcoded)
            rrsp_cap = rrsp_annual_max if rrsp_annual_max > 0 else 33810
            tfsa_lim = tfsa_annual_limit if tfsa_annual_limit > 0 else 7000
            fhsa_lim = fhsa_annual_limit if fhsa_annual_limit > 0 else _fhsa_annual_limit()  # #241: Canada package, not literal
            current_state.primary_rrsp_room += min(
                current_state.primary_income * rrsp_room_growth,
                rrsp_cap
            )
            current_state.spouse_rrsp_room += min(
                current_state.spouse_income * rrsp_room_growth,
                rrsp_cap
            )
            current_state.primary_tfsa_room += tfsa_lim
            current_state.spouse_tfsa_room += tfsa_lim
            
            # Grow FHSA room
            current_state.fhsa_room = fhsa_lim  # Annual room resets each year
            # (carry-forward of unused room handled in FHSAAccount)
            
            # Recalculate savings
            current_state.annual_savings = (
                current_state.primary_income + current_state.spouse_income
            ) * 0.20  # 20% savings rate
            
            # Allocate
            result = self.allocate(current_state)
            results.append(result)
            
            # Reduce room by amounts contributed
            current_state.primary_rrsp_room -= result.primary_rrsp
            current_state.spouse_rrsp_room -= (result.spousal_rrsp + result.spouse_rrsp)
            current_state.primary_tfsa_room -= result.primary_tfsa
            current_state.spouse_tfsa_room -= result.spouse_tfsa
            current_state.fhsa_room -= result.fhsa
            current_state.fhsa_lifetime_remaining -= result.fhsa
            
            # RESP children age out
            if year >= 2:  # Approximate: children age out
                current_state.resp_eligible_children = max(
                    0, current_state.resp_eligible_children - 1
                )
        
        return results


def create_strategy_from_config(cfg: Dict) -> AllocationStrategy:
    """Create an AllocationStrategy from input.json config.
    
    Args:
        cfg: Loaded input.json dict
    
    Returns:
        AllocationStrategy with values from config
    """
    strategy = AllocationStrategy()
    if 'savings' in cfg:
        strategy.name = f"Custom ({cfg['savings']['rate']*100:.0f}% savings)"
    return strategy


def list_strategies() -> Dict[str, AllocationStrategy]:
    """Return all registered strategies.

    Country-specific strategies register themselves into ``_STRATEGY_REGISTRY``
    at package-import time via their package ``__init__`` (DP#16 package-presence
    trigger). If the registry is empty, this triggers jurisdiction-agnostic
    country discovery so each country package is imported and self-registers.
    """
    if not _STRATEGY_REGISTRY:
        # DP#25 (issue #284): the core strategy module must not import any
        # country package directly. Trigger country auto-discovery through the
        # jurisdiction-agnostic ``countries`` registry instead — importing each
        # country package runs its registration (countries.canada → strategy),
        # so the dependency points inward, never outward.
        from countries import discover_countries
        discover_countries()
    return dict(_STRATEGY_REGISTRY)
