#!/usr/bin/env python3
"""
Asset Location Optimizer — Optimal Account Placement for Tax Efficiency

This module determines which investments should go in which accounts
to minimize tax drag. The key insight: different investment types have
different tax treatments, and different account types shelter or expose
that income differently.

    References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Federal/Provincial Tax Brackets entries
    PWL Capital / Benjamin Felix research on after-tax allocation
    Tax drag on investment income (eligible dividends, capital gains, interest):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/eligible-dividends.html
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/capital-gains.html
    CRA Folio S3-F6-C1 (interest deductibility for non-reg investments):
        https://www.canada.ca/en/revenue-agency/services/tax/technical-information/income-tax/folio-series/folio-s3/s3-f6-c1-interest-deductibility.html
    TFSA contribution room:
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html

Per DP#10: this module owns the asset location decision space.
Per DP#6: optimal placement is discovered from rules, not named.

Usage:
    from countries.canada.asset_location import AssetLocationOptimizer, PortfolioHolding
    from countries.canada.asset_location import compute_tax_drag, light_vs_ludicrous

    from tax_calculator import marginal_rate as compute_marginal_rate
    from tax_data import default_tax_provider
    brackets = default_tax_provider().get_combined_brackets(year=2026, province='quebec')
    mtr = compute_marginal_rate(150000, brackets)
    optimizer = AssetLocationOptimizer(marginal_rate=mtr, province='quebec')
    result = optimizer.optimize(portfolio)
    print(result.summary())
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from tax_calculator import (
    marginal_rate as compute_marginal_rate,
    )
from countries.canada.tax_calc import (
    withholding_tax_drag,
    asset_location_tax_impact,
    effective_dividend_rate,
)


# =============================================================================
# Account Types
# =============================================================================

class AccountType(Enum):
    RRSP = "rrsp"
    TFSA = "tfsa"
    NON_REG = "non_reg"
    RESP = "resp"
    FHSA = "fhsa"  # Future: when fhsa.py is integrated


# =============================================================================
# ETF / Investment Types
# =============================================================================

class ETFType(Enum):
    """Classification of ETF/investment by tax treatment characteristics."""
    US_LISTED_EQUITY = "us_listed"              # VTI, VXUS — WHT implications
    CANADIAN_EQUITY = "canadian"                 # XIC — no WHT, no DTC
    CANADIAN_DIVIDEND = "canadian_dividend"      # XEI, VDY — eligible dividends, DTC
    INTERNATIONAL_EQUITY = "international"        # XAW, VEE — foreign WHT
    BONDS = "bonds"                              # ZAG, VAB — interest income
    REIT = "reit"                                # XRE — distributions mix of dividends/ROC/CG


# =============================================================================
# Portfolio Holding
# =============================================================================

@dataclass
class PortfolioHolding:
    """A single holding in a portfolio.

    Stores the ETF name, type, allocation percentage, and yield.
    This is pure data (DP#8: compose through data).

    DP#2: yield_pct should come from user config (SimulationConfig.portfolio_data)
    rather than using the 0.02 default. The default exists as a DP#13 fallback
    for when no config is provided.

    Issue #691 (DP#8): the former ``mer_pct`` field was deleted here -- it was a
    second, orphaned spelling of the fund-fee fact (no engine reader; the
    asset-location module does not reach the growth engine, blocked by #641).
    The ONE canonical fee the engine consumes is the per-account ``mer`` on the
    contract account schema (input_contract -> account_mer_drag -> the growth
    rule's ``_blended_pot_rate``). A per-holding fee source, when the
    asset-location path is wired, aggregates into that account-level fee rather
    than reintroducing a parallel field.
    """
    name: str
    etf_type: ETFType
    allocation_pct: float  # e.g., 0.30 for 30%
    yield_pct: float = 0.02  # DP#13: Fallback yield rate; override from config

    @classmethod
    def from_dict(cls, data: dict) -> 'PortfolioHolding':
        """Create a PortfolioHolding from config dict (DP#2/DP#14).

        Args:
            data: Dict with keys: name, etf_type, allocation_pct, yield_pct (opt)

        Returns:
            PortfolioHolding instance
        """
        etf_type = data['etf_type'] if isinstance(data['etf_type'], ETFType) else ETFType(data['etf_type'])
        return cls(
            name=data['name'],
            etf_type=etf_type,
            allocation_pct=data['allocation_pct'],
            yield_pct=data.get('yield_pct', 0.02),
        )

    def to_dict(self) -> dict:
        """Export to dict matching input.json schema. DP#24."""
        return {
            'name': self.name,
            'etf_type': self.etf_type.value,
            'allocation_pct': self.allocation_pct,
            'yield_pct': self.yield_pct,
        }

    @property
    def distribution_type(self) -> str:
        """Primary type of distribution from this ETF."""
        type_map = {
            ETFType.US_LISTED_EQUITY: "foreign_dividend",
            ETFType.CANADIAN_EQUITY: "capital_gains_minimal",
            ETFType.CANADIAN_DIVIDEND: "eligible_dividend",
            ETFType.INTERNATIONAL_EQUITY: "foreign_dividend",
            ETFType.BONDS: "interest",
            ETFType.REIT: "mixed",
        }
        return type_map.get(self.etf_type, "mixed")


# =============================================================================
# Asset Location Result
# =============================================================================

@dataclass
class AccountAllocation:
    """Result of asset location optimization for one account."""
    account_type: AccountType
    holdings: List[PortfolioHolding] = field(default_factory=list)
    total_allocation_pct: float = 0.0

    def add(self, holding: PortfolioHolding, pct: float = None):
        """Add a holding to this account."""
        alloc = pct if pct is not None else holding.allocation_pct
        adjusted = PortfolioHolding(
            name=holding.name,
            etf_type=holding.etf_type,
            allocation_pct=alloc,
            yield_pct=holding.yield_pct,
        )
        self.holdings.append(adjusted)
        self.total_allocation_pct += alloc

    @property
    def total_value_pct(self) -> float:
        return sum(h.allocation_pct for h in self.holdings)


@dataclass
class AssetLocationResult:
    """Complete asset location optimization result."""
    allocations: Dict[AccountType, AccountAllocation] = field(default_factory=dict)
    total_tax_drag_bps: float = 0.0
    marginal_rate: float = 0.0
    province: str = "quebec"

    def summary(self) -> str:
        lines = ["📊 ASSET LOCATION OPTIMIZATION", "=" * 60]
        for acct_type in AccountType:
            if acct_type in self.allocations:
                alloc = self.allocations[acct_type]
                lines.append(f"\n  {acct_type.value.upper()}")
                for h in alloc.holdings:
                    lines.append(f"    {h.name:15s} ({h.etf_type.value:20s}): {h.allocation_pct*100:5.1f}%")
        lines.append(f"\n  Total tax drag: {self.total_tax_drag_bps:.1f} bps/year")
        lines.append(f"  Province: {self.province}")
        lines.append(f"  Marginal rate: {self.marginal_rate*100:.1f}%")
        return "\n".join(lines)

    def as_dict(self) -> Dict:
        result = {
            'total_tax_drag_bps': self.total_tax_drag_bps,
            'province': self.province,
            'marginal_rate': self.marginal_rate,
            'allocations': {},
        }
        for acct_type, alloc in self.allocations.items():
            result['allocations'][acct_type.value] = [
                {'name': h.name, 'type': h.etf_type.value, 'pct': h.allocation_pct}
                for h in alloc.holdings
            ]
        return result


# =============================================================================
# Tax Drag Calculator
# =============================================================================

def compute_tax_drag(
    holding: PortfolioHolding,
    account_type: AccountType,
    marginal_rate: float,
    province: str = "quebec",
) -> float:
    """Compute annual tax drag in basis points for a holding in an account.

    Tax drag = the annual return lost to taxes that could have been
    avoided by placing the holding in a better account.

    Args:
        holding: The portfolio holding
        account_type: Account it's placed in
        marginal_rate: Marginal tax rate
        province: Province code

    Returns:
        Tax drag in basis points per year
    """
    acct = account_type.value
    etf = holding.etf_type.value

    # WHT drag (from tax_calculator)
    wht_drag = withholding_tax_drag(acct, etf, holding.yield_pct)

    # Distribution tax drag (non-reg only)
    dist_drag = 0.0
    if account_type == AccountType.NON_REG:
        if holding.etf_type == ETFType.BONDS:
            # Bond interest is fully taxable → high drag
            dist_drag = holding.yield_pct * marginal_rate * 10000
        elif holding.etf_type == ETFType.CANADIAN_DIVIDEND:
            # Eligible dividends get DTC → lower effective rate
            eff_rate = effective_dividend_rate(marginal_rate, province)
            dist_drag = holding.yield_pct * eff_rate * 10000
        elif holding.etf_type in (ETFType.US_LISTED_EQUITY, ETFType.INTERNATIONAL_EQUITY):
            # Foreign dividends: WHT covered by FTC, but fully taxable at MTR
            # WHT drag already counted above, add income tax drag for non-reg
            dist_drag = holding.yield_pct * marginal_rate * 10000
            # FTC offsets WHT, so we've double-counted — correct
            dist_drag = wht_drag  # WHT drag already captures this for non-reg
            wht_drag = 0  # Don't double-count

    return wht_drag + dist_drag


# =============================================================================
# Asset Location Optimizer
# =============================================================================

class AssetLocationOptimizer:
    """Determines optimal asset location for a portfolio.

    Per DP#6: the optimal placement is discovered from rules (tax drag),
    not named by convention.

    Two modes:
    - Light: same allocation in all accounts (easy to maintain)
    - Ludicrous: per-account placement for maximum tax efficiency
    """

    def __init__(self, marginal_rate: float = 0.0,
                 province: str = "quebec",
                 account_sizes: Dict[AccountType, float] = None):
        """
        Args:
            marginal_rate: Combined marginal tax rate
            province: Province code (affects QC DTC for dividends)
            account_sizes: Dict of AccountType → proportion of total portfolio
                           e.g., {RRSP: 0.35, TFSA: 0.20, NON_REG: 0.45}
                           DP#2: Should come from SimulationConfig.account_sizes when available.
        """
        self.marginal_rate = marginal_rate
        self.province = province
        # DP#2: Default account sizes are DP#13 fallbacks. Override from config.
        self.account_sizes = account_sizes or {
            AccountType.RRSP: 0.35,
            AccountType.TFSA: 0.20,
            AccountType.NON_REG: 0.45,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'AssetLocationOptimizer':
        """Create optimizer from config dict (DP#2/DP#14).

        Args:
            data: Dict with optional keys: marginal_rate, province, account_sizes
                  account_sizes uses string keys ('rrsp', 'tfsa', 'non_reg')
                  which are converted to AccountType enums.
        """
        account_sizes = None
        if 'account_sizes' in data:
            account_sizes = {
                AccountType(k): v for k, v in data['account_sizes'].items()
            }
        return cls(
            marginal_rate=data.get('marginal_rate', 0.0),
            province=data.get('province', 'quebec'),
            account_sizes=account_sizes,
        )

    def to_dict(self) -> dict:
        """Export to dict matching input.json schema. DP#24."""
        return {
            'marginal_rate': self.marginal_rate,
            'province': self.province,
            'account_sizes': {
                k.value: v for k, v in self.account_sizes.items()
            },
        }

    def optimize(self, portfolio: List[PortfolioHolding]) -> AssetLocationResult:
        """Optimize asset location for maximum after-tax returns.

        Uses the "ludicrous" approach: place each holding in the account
        with the lowest tax drag, respecting account size constraints.

        Args:
            portfolio: List of portfolio holdings

        Returns:
            AssetLocationResult with optimal placement
        """
        result = AssetLocationResult(
            marginal_rate=self.marginal_rate,
            province=self.province,
        )

        # Compute tax drag for each holding × account combination
        drag_matrix = []
        for holding in portfolio:
            for acct_type in self.account_sizes:
                drag = compute_tax_drag(holding, acct_type, self.marginal_rate, self.province)
                drag_matrix.append({
                    'holding': holding,
                    'account': acct_type,
                    'drag_bps': drag,
                })

        # Sort by drag (lowest first) and assign greedily
        # Per DP#30: use tax impact data, not prescriptive advice
        for holding in portfolio:
            tax_impact = asset_location_tax_impact(
                holding.etf_type.value, self.marginal_rate, self.province,
                yield_pct=holding.yield_pct,
            )
            # Map string names to AccountType, sorted by lowest tax drag first
            acct_order = []
            for acct_name in sorted(tax_impact, key=tax_impact.get):
                try:
                    acct_order.append(AccountType(acct_name))
                except ValueError:
                    continue

            # Place in first available account with room
            placed = False
            for acct_type in acct_order:
                if acct_type in self.account_sizes:
                    if acct_type not in result.allocations:
                        result.allocations[acct_type] = AccountAllocation(account_type=acct_type)
                    alloc = result.allocations[acct_type]
                    if alloc.total_value_pct < self.account_sizes[acct_type]:
                        alloc.add(holding)
                        placed = True
                        break

            if not placed:
                # Fallback: place in non-reg
                if AccountType.NON_REG not in result.allocations:
                    result.allocations[AccountType.NON_REG] = AccountAllocation(account_type=AccountType.NON_REG)
                result.allocations[AccountType.NON_REG].add(holding)

        # Compute total tax drag
        total_drag = 0.0
        for acct_type, alloc in result.allocations.items():
            for h in alloc.holdings:
                drag = compute_tax_drag(h, acct_type, self.marginal_rate, self.province)
                total_drag += drag * h.allocation_pct

        result.total_tax_drag_bps = total_drag
        return result

    def light_approach(self, portfolio: List[PortfolioHolding]) -> AssetLocationResult:
        """Same allocation in all accounts (easier to maintain).

        This is the "light" approach from the PWL Capital research.
        Tax drag is higher but rebalancing is much simpler.
        """
        result = AssetLocationResult(
            marginal_rate=self.marginal_rate,
            province=self.province,
        )

        for acct_type, size in self.account_sizes.items():
            alloc = AccountAllocation(account_type=acct_type)
            for holding in portfolio:
                alloc.add(holding, pct=holding.allocation_pct * size)
            result.allocations[acct_type] = alloc

        # Compute total drag
        total_drag = 0.0
        for acct_type, alloc in result.allocations.items():
            for h in alloc.holdings:
                drag = compute_tax_drag(h, acct_type, self.marginal_rate, self.province)
                total_drag += drag * h.allocation_pct

        result.total_tax_drag_bps = total_drag
        return result


# =============================================================================
# Comparison: Light vs Ludicrous
# =============================================================================

def portfolio_from_config(config: dict) -> List[PortfolioHolding]:
    """Create a portfolio from SimulationConfig portfolio data (DP#2).

    Reads portfolio composition from the config's 'holdings' section.
    Each holding specifies name, etf_type, allocation_pct, and optionally
    yield_pct.

    Args:
        config: Dict with 'holdings' key containing list of holding dicts,
                or a flat dict with 'portfolio' key wrapping the holdings.

    Returns:
        List of PortfolioHolding instances created from config data.
    """
    holdings_data = config.get('holdings', config.get('portfolio', []))
    if not holdings_data:
        return []
    return [PortfolioHolding.from_dict(h) for h in holdings_data]


def light_vs_ludicrous(
    portfolio: List[PortfolioHolding],
    marginal_rate: float = 0.0,
    province: str = "quebec",
    account_sizes: Dict[AccountType, float] = None,
    portfolio_value: float = 500000,
) -> Dict:
    """Compare the light vs ludicrous asset location approaches.

    Args:
        portfolio: Portfolio holdings
        marginal_rate: Marginal tax rate
        province: Province
        account_sizes: Account sizes (proportions)
        portfolio_value: Total portfolio value in dollars

    Returns:
        Dict with comparison results
    """
    optimizer = AssetLocationOptimizer(marginal_rate, province, account_sizes)

    light = optimizer.light_approach(portfolio)
    ludicrous = optimizer.optimize(portfolio)

    # Convert bps drag to dollar cost
    light_cost = light.total_tax_drag_bps / 10000 * portfolio_value
    ludicrous_cost = ludicrous.total_tax_drag_bps / 10000 * portfolio_value
    annual_savings = light_cost - ludicrous_cost

    return {
        'light_drag_bps': light.total_tax_drag_bps,
        'ludicrous_drag_bps': ludicrous.total_tax_drag_bps,
        'savings_bps': light.total_tax_drag_bps - ludicrous.total_tax_drag_bps,
        'light_annual_cost': light_cost,
        'ludicrous_annual_cost': ludicrous_cost,
        'annual_savings': annual_savings,
        'ten_year_savings': annual_savings * 10,
        'light_allocation': light.as_dict(),
        'ludicrous_allocation': ludicrous.as_dict(),
        'recommendation': 'ludicrous' if ludicrous.total_tax_drag_bps < light.total_tax_drag_bps else 'light',
    }