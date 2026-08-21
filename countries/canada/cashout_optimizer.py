#!/usr/bin/env python3
"""
Cash-Out Optimizer — Minimum Extraction Calculator

Answers the critical question: "How much do I actually need to borrow?"

Instead of "borrow the max (80% LTV) and allocate", this computes:
1. What registered accounts need to be filled (room × tax benefit)
2. How much margin is already available
3. The minimum cash-out to fill the gap
4. Whether borrowing extra for non-reg investment is worth it

This is the fix for the circular logic bug: refinancing to 80% LTV
just to pay the money back as mortgage paydown makes no sense.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RRSP/TFSA/FHSA entries
    RRSP contribution limits and deduction rules (ITA s.146):
        https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html
    TFSA contribution room:
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html
    OSFI Guideline B-20 (mortgage underwriting, LTV limits):
        https://www.osfi-bsif.gc.ca/Home/Blog/2024/2024-guideline-b20

Usage:
    from countries.canada.cashout_optimizer import CashOutPlan, compute_min_extraction
    plan = compute_min_extraction(cfg, brackets)
    print(f"Need ${plan.required_cashout:,.0f} → {plan.required_ltv:.1%} LTV")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tax_calculator import (
    marginal_rate, tax_on_income,
)
from tax_data import default_tax_provider
from countries.canada.tax_calc import (
    rrsp_deduction_savings, rrsp_deduct_later_savings,
)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class AccountNeed:
    """A single account that needs funding."""
    account: str           # e.g. "Primary RRSP", "Spouse RRSP"  (DP#4: role-based)
    role: str              # "primary", "spouse"
    room: float            # Available contribution room
    priority: int          # 1=must fill, 2=high value, 3=nice-to-have
    refund_rate: float      # Effective refund rate for this contribution
    refund_amount: float    # $ refund from contributing this room
    source: str             # "margin", "cashout", or "refund"
    benefit_per_dollar: float  # 10-year after-tax return per $1 contributed


@dataclass
class CashOutPlan:
    """Complete cash-out optimization result.
    
    This is a pure data object — no logic. All fields are computed
    by compute_min_extraction().
    """
    # What we need to fund
    account_needs: List[AccountNeed] = field(default_factory=list)
    total_registered_room: float = 0.0
    
    # Sources of funds
    margin_available: float = 0.0
    cashout_required: float = 0.0    # Minimum cash-out needed
    refund_available: float = 0.0    # Refunds from RRSP contributions
    
    # LTV calculations
    current_mortgage: float = 0.0
    house_value: float = 0.0
    required_ltv: float = 0.0       # Minimum LTV to fund registered accounts
    max_ltv: float = 0.80
    
    # Allocation waterfall
    margin_to_registered: float = 0.0
    cashout_to_registered: float = 0.0
    refund_to_paydown: float = 0.0   # Refund used to pay down debt
    
    # Remaining after filling registered accounts
    excess_cashout: float = 0.0      # If borrowing beyond minimum
    net_debt_after_refund: float = 0.0
    
    # Decision support
    nonreg_spread: float = 0.0       # After-tax return - after-tax cost for non-reg
    paydown_guaranteed: float = 0.0  # After-tax return from mortgage paydown
    recommendation: str = ""

    # §20(1)(c) deductibility risk flag: readvanceable structure is present but
    # the SM investment is zero-yield, so the federal interest deduction is not
    # established (no reasonable expectation of income).
    deductibility_risk: str = ""
    
    # Per-LTV comparison
    ltv_levels: List[Dict] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        """Export plan as a JSON-serializable dict.
        
        DP#24: CashOutPlan round-trips for saving and inspection.
        """
        return {
            'account_needs': [
                {'account': n.account, 'role': n.role, 'room': n.room,
                 'priority': n.priority, 'refund_rate': n.refund_rate,
                 'refund_amount': n.refund_amount, 'source': n.source,
                 'benefit_per_dollar': n.benefit_per_dollar}
                for n in self.account_needs
            ],
            'total_registered_room': self.total_registered_room,
            'margin_available': self.margin_available,
            'cashout_required': self.cashout_required,
            'refund_available': self.refund_available,
            'current_mortgage': self.current_mortgage,
            'house_value': self.house_value,
            'required_ltv': self.required_ltv,
            'max_ltv': self.max_ltv,
            'margin_to_registered': self.margin_to_registered,
            'cashout_to_registered': self.cashout_to_registered,
            'refund_to_paydown': self.refund_to_paydown,
            'excess_cashout': self.excess_cashout,
            'net_debt_after_refund': self.net_debt_after_refund,
            'nonreg_spread': self.nonreg_spread,
            'paydown_guaranteed': self.paydown_guaranteed,
            'recommendation': self.recommendation,
            'deductibility_risk': self.deductibility_risk,
        }


# =============================================================================
# Pure Functions — Cash-Out Optimization
# =============================================================================

def compute_per_dollar_benefit(
    contribution: float,
    refund_rate: float,
    investment_return: float | None = None,
    years: int = 10,
    withdrawal_tax_rate: float = 0.30,
) -> float:
    """Compute 10-year after-tax benefit per $1 contributed to RRSP.
    
    Per $1:
    - Immediate refund: $refund_rate
    - Tax-free growth for 10 years: $(1 + r)^10
    - Withdrawal tax: -$(1+r)^10 × withdrawal_rate
    - Net = refund_rate + (1+r)^10 × (1 - withdrawal_rate)
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    growth = (1 + investment_return) ** years
    return refund_rate + growth * (1 - withdrawal_tax_rate)


def compute_tfsa_per_dollar(
    investment_return: float | None = None,
    years: int = 10,
) -> float:
    """Compute 10-year after-tax benefit per $1 contributed to TFSA.
    
    Per $1: (1 + r)^10 — fully tax-free growth and withdrawal.
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    return (1 + investment_return) ** years


def compute_fhsa_per_dollar(
    contribution: float,
    refund_rate: float,
    investment_return: float | None = None,
    years: int = 10,
    qualifying_withdrawal: bool = True,
) -> float:
    """Compute 10-year after-tax benefit per $1 contributed to FHSA.
    
    FHSA provides a DUAL benefit (DP#10: FHSA rules):
    1. Immediate deduction (like RRSP): $refund_rate per $1 contributed
    2. Tax-free qualifying withdrawal (like TFSA): no tax on withdrawal
    
    For qualifying first-home withdrawals, FHSA is strictly better than RRSP
    because you get the deduction AND tax-free withdrawal.
    
    Per $1:
    - Immediate refund: $refund_rate
    - Tax-free growth for 10 years: $(1+r)^10
    - Tax-free withdrawal (qualifying): full amount
    - Net = refund_rate + (1+r)^10
    
    For non-qualifying withdrawals (taxed as income):
    - Net = refund_rate + (1+r)^10 * (1 - withdrawal_tax_rate)
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    growth = (1 + investment_return) ** years
    if qualifying_withdrawal:
        # Dual benefit: deduction + tax-free withdrawal
        return refund_rate + growth
    else:
        # Non-qualifying: like RRSP (deduction but taxed on withdrawal)
        return refund_rate + growth * (1 - 0.30)  # Assume ~30% withdrawal tax


def compute_nonreg_per_dollar(
    investment_return: float | None = None,
    years: int = 10,
    capital_gains_inclusion: float = 0.50,
    marginal_rate: float = 0.50,
) -> float:
    """Compute 10-year after-tax benefit per $1 in non-reg.
    
    Growth at full rate, but capital gains taxed on distribution.
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    growth = (1 + investment_return) ** years
    gains = growth - 1
    tax = gains * capital_gains_inclusion * marginal_rate
    return growth - tax


def compute_paydown_per_dollar(
    mortgage_rate: float = 0.0,  # DP#13: set from config; typical 2026: 0.0495
    years: int = 10,
) -> float:
    """Compute 10-year benefit per $1 of mortgage paydown.
    
    This is guaranteed: each dollar paid down saves mortgage_rate
    in interest, compounded over the period.
    """
    return (1 + mortgage_rate) ** years


def _sm_readvanceable(cfg: Dict) -> bool:
    """Whether the mortgage is readvanceable (the SM structural precondition).

    A readvanceable / re-advanceable mortgage (a combined mortgage-HELOC account
    whose revolving credit limit grows as principal is repaid) is the precondition
    the rest of this tool relies on for the Smith Manoeuvre / cash damming. This
    is purpose/structure only — it does NOT, on its own, establish §20(1)(c)
    deductibility (see `_sm_deductible`).
    """
    # DP#9 (#621): 'property.readvanceable' was an undocumented third
    # spelling of this flag (present in neither schema file). Deleted in
    # favor of 'property.heloc_readvance', the documented, engine-wide "live
    # spelling" (see simulation.py's SimulationConfig.is_readvanceable,
    # scenario_discovery.py, strategies.py, rate_model.py, and
    # test_schema_coverage.py's own annotation). An explicit
    # heloc_readvance=False is now respected rather than silently overridden.
    prop = cfg.get('property', {})
    return bool(prop.get('heloc_readvance'))


def _sm_yield_rate(cfg: Dict) -> float:
    """Expected income/distribution yield on the SM non-reg investment."""
    return cfg.get('assumptions', {}).get('non_reg_yield_rate', 0.0)


def _sm_deductible(cfg: Dict) -> bool:
    """Whether the cash-out borrowing qualifies as deductible (Smith Manoeuvre).

    Interest on funds borrowed to earn investment income is deductible under
    CRA §20(1)(c) only when BOTH conditions hold:
      1. The borrowing is readvanceable and the proceeds are traced to
         non-registered investments (structure / purpose — `_sm_readvanceable`).
      2. The investment has a reasonable expectation of income, i.e. a positive
         distribution yield. A pure-growth, zero-yield holding fails the
         income-producing test and the interest does NOT qualify federally.

    Note: a positive yield is a necessary (not sufficient) proxy for "reasonable
    expectation of income"; this tool models the yield it is given as the income
    signal (DP#13: the user supplies the assumption).
    """
    return _sm_readvanceable(cfg) and _sm_yield_rate(cfg) > 0


def compute_min_extraction(
    cfg: Dict,
    brackets: List[Dict] = None,
    investment_return: float | None = None,
    mortgage_rate: float = None,
    years: int = 10,
    explore_ltv_levels: bool = True,
    provider: 'TaxDataProvider' = None,
) -> CashOutPlan:
    """Compute the minimum cash-out needed to fill registered accounts.
    
    This is the core function that fixes the "borrow max then allocate" bug.
    Instead, it computes "borrow what you need, no more".
    
    Args:
        cfg: Loaded input.json config dict
        brackets: Tax brackets (loaded if not provided)
        investment_return: Expected investment return
        mortgage_rate: Current mortgage rate (from cfg if not provided)
        years: Projection period
        explore_ltv_levels: Whether to compute per-LTV comparisons
    
    Returns:
        CashOutPlan with complete optimization result
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    if brackets is None:
        if provider is None:
            provider = default_tax_provider()
        brackets = provider.get_combined_brackets()
    
    # Extract config values
    house_value = cfg['property']['house_value']
    current_mortgage = cfg['property']['mortgage_balance']
    margin_available = cfg['property'].get('margin_available', 0)
    max_ltv = cfg['property'].get('ltv_max', 0.80)
    if mortgage_rate is None:
        mortgage_rate = cfg['property']['mortgage_rate']
    
    # Family members
    members = cfg.get('family', {}).get('members', [])
    primary = next((m for m in members if m['role'] == 'primary'), {})
    spouse = next((m for m in members if m['role'] == 'spouse'), {})
    
    primary_income = primary.get('gross_income', 0)  # DP#13: personal data, not a default
    spouse_income = spouse.get('gross_income', 0)
    n_rrsp_room = primary.get('rrsp_room_accumulated', 0)
    a_rrsp_room = spouse.get('rrsp_room_accumulated', 0)
    n_tfsa_room = primary.get('tfsa_room_accumulated', 0)
    a_tfsa_room = spouse.get('tfsa_room_accumulated', 0)
    # FHSA room (DP#69: FHSA dual benefit)
    n_fhsa_room = primary.get('fhsa_room_accumulated', 0)
    a_fhsa_room = spouse.get('fhsa_room_accumulated', 0)
    total_fhsa_room = n_fhsa_room + a_fhsa_room
    primary_mtr = marginal_rate(primary_income, brackets)
    spouse_mtr = marginal_rate(spouse_income, brackets)
    
    # Compute RRSP refunds (properly, using bracket-aware calculation)
    # Primary: deduct-later (only to bracket target)
    bracket_target = cfg.get('assumptions', {}).get('deduct_later_bracket_target', 0)  # DP#13: 0 = auto-detect
    # Auto-detect bracket target from tax brackets when not set
    if bracket_target == 0 and primary_income > 0:
        for i in range(len(brackets) - 1, 0, -1):
            if brackets[i]['min'] < primary_income and brackets[i]['rate'] >= primary_mtr - 0.10:
                bracket_target = brackets[i]['min']
                break
        if bracket_target == 0:
            bracket_target = brackets[3]['min'] if len(brackets) > 3 else 50000
    primary_deduction_need = max(0, primary_income - bracket_target)
    n_deduct_amount = min(n_rrsp_room, primary_deduction_need)
    primary_refund = rrsp_deduction_savings(n_deduct_amount, primary_income, brackets)
    n_undeducted = n_rrsp_room - n_deduct_amount
    
    # Spouse: deducts all at her MTR
    a_refund = a_rrsp_room * spouse_mtr
    
    total_refund = primary_refund + a_refund + (total_fhsa_room * primary_mtr if total_fhsa_room > 0 else 0)
    
    # Per-$1 benefit calculations
    rrsp_n_bpd = compute_per_dollar_benefit(n_deduct_amount, primary_refund / max(1, n_deduct_amount),
                                              investment_return, years)
    rrsp_a_bpd = compute_per_dollar_benefit(a_rrsp_room, spouse_mtr,
                                              investment_return, years)
    
    # FHSA dual benefit: deduction + tax-free withdrawal (DP#10, DP#69)
    fhsa_bpd = compute_fhsa_per_dollar(total_fhsa_room, primary_mtr, investment_return, years)
    
    tfsa_bpd = compute_tfsa_per_dollar(investment_return, years)
    nonreg_bpd = compute_nonreg_per_dollar(investment_return, years, 0.50, primary_mtr)
    paydown_bpd = compute_paydown_per_dollar(mortgage_rate, years)
    
    # Build account needs (priority-ordered waterfall)
    account_needs = []
    
    # Priority 1: FHSA (dual benefit: deduction + tax-free withdrawal)
    # FHSA is strictly better than TFSA for first-home buyers (DP#69).
    # It provides both an immediate tax deduction (like RRSP) AND
    # tax-free withdrawal for first-home purchase (like TFSA).
    # For qualifying withdrawals, benefit_per_dollar = refund_rate + growth.
    if total_fhsa_room > 0:
        fhsa_refund = total_fhsa_room * primary_mtr  # FHSA deduction at primary MTR
        account_needs.append(AccountNeed(
            account="FHSA (both)", role="both",
            room=total_fhsa_room, priority=1,
            refund_rate=primary_mtr,
            refund_amount=fhsa_refund,
            source="margin",
            benefit_per_dollar=fhsa_bpd,
        ))
    
    # Priority 2: Primary RRSP (highest refund rate, deduct-later)
    if n_rrsp_room > 0:
        account_needs.append(AccountNeed(
            account="Primary RRSP", role="primary",
            room=n_rrsp_room, priority=1,
            refund_rate=primary_refund / max(1, n_deduct_amount),
            refund_amount=primary_refund,
            source="margin",  # Filled from margin first
            benefit_per_dollar=rrsp_n_bpd,
        ))
    
    # Priority 3: TFSA (tax-free, no refund but no future tax)
    tfsa_room = n_tfsa_room + a_tfsa_room
    if tfsa_room > 0:
        account_needs.append(AccountNeed(
            account="TFSA (both)", role="both",
            room=tfsa_room, priority=1,
            refund_rate=0, refund_amount=0,
            source="margin",
            benefit_per_dollar=tfsa_bpd,
        ))
    
    # Priority 4: Spouse RRSP (lower refund rate, but still great)
    if a_rrsp_room > 0:
        account_needs.append(AccountNeed(
            account="Spouse RRSP", role="spouse",
            room=a_rrsp_room, priority=2,
            refund_rate=spouse_mtr,
            refund_amount=a_refund,
            source="cashout",  # Needs refinancing
            benefit_per_dollar=rrsp_a_bpd,
        ))
    
    total_registered_room = sum(n.room for n in account_needs)
    
    # Allocation waterfall: margin first, then cash-out
    margin_remaining = margin_available
    cashout_to_registered = 0.0
    margin_to_registered = 0.0
    
    for need in account_needs:
        if margin_remaining > 0:
            fill_from_margin = min(need.room, margin_remaining)
            margin_to_registered += fill_from_margin
            margin_remaining -= fill_from_margin
            need.source = "margin"
            if fill_from_margin < need.room:
                # Overflow goes to cashout
                overflow = need.room - fill_from_margin
                cashout_to_registered += overflow
                need.source = "margin + cashout"
        else:
            cashout_to_registered += need.room
            need.source = "cashout"
    
    # Minimum cash-out required
    required_cashout = cashout_to_registered
    required_ltv = (current_mortgage + required_cashout) / house_value if house_value > 0 else 0
    
    # After refunds arrive, use them to pay down margin/debt
    refund_to_paydown = total_refund
    net_debt = (margin_to_registered + cashout_to_registered) - refund_to_paydown
    
    # At max LTV, how much excess cash would we have?
    max_cashout = house_value * max_ltv - current_mortgage
    excess_cashout = max(0, max_cashout - required_cashout)
    
    # Spread analysis for non-reg investment vs mortgage paydown.
    #
    # The cash-out here is borrowed against a readvanceable mortgage and invested
    # in income-producing non-registered assets (the Smith Manoeuvre / cash
    # damming this whole tool models). Under CRA §20(1)(c), interest on money
    # borrowed to earn investment income IS tax-deductible, so the real cost of
    # carrying that debt is the after-tax rate, not the gross mortgage rate.
    #
    # This mirrors the authoritative simulation, which uses the same after-tax
    # HELOC cost = heloc_rate × (1 − MTR) (see simulate.py and
    # simulation_state.py readvance_tax_savings). Keeping the same one-liner here
    # ensures the advisory and the engine can't drift in sign or magnitude.
    nonreg_after_tax_return = investment_return * (1 - primary_mtr * 0.50)  # CG taxed
    if _sm_deductible(cfg):
        # Deductible (SM / readvanceable): §20(1)(c) reduces the carrying cost.
        nonreg_after_tax_cost = mortgage_rate * (1 - primary_mtr)
    else:
        # Non-deductible path: borrowed funds not traced to income-producing
        # investment, so the full mortgage rate is the carrying cost.
        nonreg_after_tax_cost = mortgage_rate
    nonreg_spread = nonreg_after_tax_return - nonreg_after_tax_cost
    paydown_guaranteed = mortgage_rate  # Guaranteed return from paying down mortgage
    
    # Build plan
    plan = CashOutPlan(
        account_needs=account_needs,
        total_registered_room=total_registered_room,
        margin_available=margin_available,
        cashout_required=required_cashout,
        refund_available=total_refund,
        current_mortgage=current_mortgage,
        house_value=house_value,
        required_ltv=required_ltv,
        max_ltv=max_ltv,
        margin_to_registered=margin_to_registered,
        cashout_to_registered=cashout_to_registered,
        refund_to_paydown=refund_to_paydown,
        excess_cashout=excess_cashout,
        net_debt_after_refund=net_debt,
        nonreg_spread=nonreg_spread,
        paydown_guaranteed=paydown_guaranteed,
    )

    # §20(1)(c) deductibility risk: readvanceable structure but zero-yield holding.
    # The interest carrying-cost above is therefore treated as non-deductible (full
    # rate), and we surface the reason so the user can correct the assumption.
    if _sm_readvanceable(cfg) and _sm_yield_rate(cfg) <= 0:
        plan.deductibility_risk = (
            "Readvanceable structure present, but the SM investment is zero-yield "
            "(no reasonable expectation of income): interest is NOT deductible under "
            "CRA §20(1)(c). Federal carrying cost uses the full mortgage rate."
        )

    # Recommendation
    if nonreg_spread > 0.01:
        plan.recommendation = (
            f"Fill registered accounts (min LTV {required_ltv:.1%}), "
            f"then invest excess in non-reg (spread {nonreg_spread:.2%})"
        )
    elif nonreg_spread > 0:
        plan.recommendation = (
            f"Fill registered accounts (min LTV {required_ltv:.1%}). "
            f"Non-reg spread is thin ({nonreg_spread:.2%}) — "
            f"mortgage paydown ({paydown_guaranteed:.2%} guaranteed) may be safer."
        )
    else:
        plan.recommendation = (
            f"Fill registered accounts (min LTV {required_ltv:.1%}), "
            f"then pay down mortgage ({paydown_guaranteed:.2%} guaranteed). "
            f"Non-reg spread is negative ({nonreg_spread:.2%}) — not worth the risk."
        )
    
    # Per-LTV level comparison
    if explore_ltv_levels:
        ltv_levels = _compute_ltv_levels(
            plan, investment_return, mortgage_rate, primary_mtr, years,
        )
        plan.ltv_levels = ltv_levels
    
    return plan


def _compute_ltv_levels(
    plan: CashOutPlan,
    investment_return: float,
    mortgage_rate: float,
    primary_mtr: float,
    years: int,
    ltv_steps: List[float] = None,
) -> List[Dict]:
    """Compute outcomes at each LTV level.
    
    At each LTV, the cash-out is split between:
    1. Registered accounts (fixed amount = total_registered_room)
    2. Remaining → mortgage paydown or non-reg investment
    
    The key insight: at minimum LTV, you borrow exactly what you need.
    At higher LTVs, you borrow extra that must either earn more than
    the mortgage costs or be paid back (circular).
    """
    if ltv_steps is None:
        ltv_steps = [0.0, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
    
    levels = []
    tfsa_bpd = compute_tfsa_per_dollar(investment_return, years)
    nonreg_bpd = compute_nonreg_per_dollar(investment_return, years, 0.50, primary_mtr)
    paydown_bpd = compute_paydown_per_dollar(mortgage_rate, years)
    
    registered_fv = plan.total_registered_room * (1 + investment_return) ** years
    
    for ltv in ltv_steps:
        cashout = max(0, plan.house_value * ltv - plan.current_mortgage)
        
        if cashout <= 0:
            # No refinance — just use margin
            total_invested = min(plan.margin_available, plan.total_registered_room)
            excess = 0
            debt_from_cashout = 0
        else:
            total_invested = plan.total_registered_room
            excess = max(0, cashout - plan.cashout_required)
            debt_from_cashout = cashout
        
        # Excess allocation: split between paydown and non-reg
        # At 0% LTV: no excess, no decision
        # At 80% LTV: significant excess — what to do with it?
        if excess > 0:
            # Option A: Pay down mortgage (circular but guaranteed)
            paydown_benefit = excess * paydown_bpd - excess  # Net of borrowing cost
            # The circular logic: borrow $X at mortgage_rate, pay it back immediately
            # Net benefit = 0 (you just moved money in a circle)
            circular_net = 0
            
            # Option B: Invest in non-reg (risky spread)
            nonreg_fv = excess * nonreg_bpd
            nonreg_interest_cost = excess * (1 + mortgage_rate) ** years
            nonreg_net = nonreg_fv - nonreg_interest_cost
            
            # Option C: Keep as available HELOC room (flexibility)
            # No cost, no benefit — just available capacity
        else:
            nonreg_net = 0
            circular_net = 0
        
        total_debt = plan.current_mortgage + debt_from_cashout - plan.refund_to_paydown
        
        levels.append({
            'ltv': ltv,
            'cashout': cashout,
            'registered_invested': min(plan.margin_available + cashout, plan.total_registered_room),
            'excess': excess,
            'nonreg_net_benefit': nonreg_net if excess > 0 else 0,
            'total_debt': total_debt,
            'registered_fv': registered_fv,
        })
    
    return levels


def print_cashout_report(plan: CashOutPlan) -> None:
    """Print a formatted cash-out optimization report."""
    print("\n" + "=" * 80)
    print("💰 CASH-OUT OPTIMIZER — Minimum Extraction Analysis")
    print("=" * 80)
    
    # Per-$1 comparison
    print(f"\n  📊 PER-$1 COMPARISON (10-year after-tax return)")
    print(f"  {'─' * 50}")
    for need in plan.account_needs:
        print(f"  {need.account:<20s} ${need.benefit_per_dollar:>5.2f}  "
              f"(refund: {need.refund_rate:.0%})  [{need.source}]")
    print(f"  {'Mortgage paydown':<20s} ${compute_paydown_per_dollar():>5.2f}  (guaranteed)")
    print(f"  {'Non-reg investment':<20s} ${compute_nonreg_per_dollar(0.07):>5.2f}  (risky)")
    
    # Allocation waterfall
    print(f"\n  📋 ALLOCATION WATERFALL")
    print(f"  {'─' * 50}")
    print(f"  Total registered room:     ${plan.total_registered_room:>12,.0f}")
    print(f"  Margin available:          ${plan.margin_available:>12,.0f}")
    margin_gap = max(0, plan.total_registered_room - plan.margin_available)
    print(f"  Gap (need cash-out):       ${margin_gap:>12,.0f}")
    print(f"  Minimum cash-out required: ${plan.cashout_required:>12,.0f}")
    print(f"  Required LTV:              {plan.required_ltv:>11.1%}")
    
    # Refund analysis
    print(f"\n  💵 RRSP REFUND ANALYSIS")
    print(f"  {'─' * 50}")
    for need in plan.account_needs:
        if need.refund_amount > 0:
            print(f"  {need.account:<20s} ${need.refund_amount:>12,.0f}  "
                  f"(eff rate: {need.refund_rate:.1%})")
    print(f"  {'Total refund':<20s} ${plan.refund_available:>12,.0f}")
    print(f"  Refund → pay down debt (guaranteed {plan.paydown_guaranteed:.2%} return)")
    
    # After refund
    print(f"\n  📈 AFTER REFUNDS")
    print(f"  {'─' * 50}")
    print(f"  Total borrowed:           ${plan.margin_to_registered + plan.cashout_to_registered:>12,.0f}")
    print(f"  Less: refund paydown:     ${plan.refund_to_paydown:>12,.0f}")
    print(f"  Net debt:                ${plan.net_debt_after_refund:>12,.0f}")
    
    # Excess analysis
    if plan.excess_cashout > 0:
        print(f"\n  ⚠️  EXCESS CASH-OUT AT 80% LTV")
        print(f"  {'─' * 50}")
        print(f"  80% LTV cash-out:         ${plan.house_value * plan.max_ltv - plan.current_mortgage:>12,.0f}")
        print(f"  Actually needed:          ${plan.cashout_required:>12,.0f}")
        print(f"  Excess (circular):       ${plan.excess_cashout:>12,.0f}")
        print(f"  Non-reg spread:           {plan.nonreg_spread:>11.2%}  (risky)")
        print(f"  Mortgage paydown:        {plan.paydown_guaranteed:>11.2%}  (guaranteed)")
        if plan.nonreg_spread <= 0:
            print(f"  ⚠️  Non-reg spread is NEGATIVE — borrowing extra loses money!")
    
    # LTV levels
    if plan.ltv_levels:
        print(f"\n  📊 LTV LEVEL COMPARISON")
        print(f"  {'─' * 80}")
        print(f"  {'LTV':>5s}  {'Cash-out':>10s}  {'Registered':>12s}  {'Excess':>10s}  "
              f"{'Net Debt':>10s}  {'Reg FV':>10s}")
        for lvl in plan.ltv_levels:
            print(f"  {lvl['ltv']:>4.0%}  ${lvl['cashout']:>9,.0f}  "
                  f"${lvl['registered_invested']:>11,.0f}  "
                  f"${lvl['excess']:>9,.0f}  "
                  f"${lvl['total_debt']:>9,.0f}  "
                  f"${lvl['registered_fv']:>9,.0f}")
    
    # Deductibility risk (§20(1)(c) income-producing test)
    if plan.deductibility_risk:
        print("\n  ⚠️  DEDUCTIBILITY RISK (CRA §20(1)(c))")
        print(f"  {'─' * 50}")
        print(f"  {plan.deductibility_risk}")

    # Recommendation
    print(f"\n  💡 RECOMMENDATION")
    print(f"  {'─' * 50}")
    print(f"  {plan.recommendation}")
    print()