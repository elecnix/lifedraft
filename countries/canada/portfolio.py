#!/usr/bin/env python3
"""
Portfolio Composition — Per-Account, Per-Income-Type Investment Modeling (DP#27)

Tax law treats five income types differently:
1. Interest — fully taxable at marginal rate
2. Canadian eligible dividends — 38% gross-up + DTC
3. Canadian non-eligible dividends — 15% gross-up + lower DTC
4. Capital gains — 50% inclusion (deferred until realized)
5. Foreign income — fully taxable + withholding tax varies by account
6. Return of capital — not taxed, reduces ACB

This module models portfolio composition as data (DP#8), computes
after-tax returns per account type, and determines optimal asset location.

DP#30: The simulator models tax consequences, not financial decisions.
Input provides allocation; simulator computes after-tax outcome.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Dividend Tax Credits, Foreign Withholding entries
    ITA s.82 (dividend gross-up), s.121 (DTC), s.38-40 (capital gains)
    PWL Capital "Foreign Withholding Taxes" (Justin Bender, 2016)
    BMO Tax Tips for Investors 2026

Usage:
    from countries.canada.portfolio import PortfolioConfig, AccountComposition, after_tax_return_by_account
    
    portfolio = PortfolioConfig.from_dict(cfg['portfolio'])
    mtr = compute_marginal_rate(150000, brackets=default_tax_provider().get_combined_brackets(year=2026, province='quebec'))
    tax_drag = portfolio.tax_drag_by_account('tfsa', marginal_rate=mtr)
    after_tax = portfolio.after_tax_return('non_reg', marginal_rate=mtr)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from countries.canada.income_type import (
    IncomeType, effective_tax_rate, wht_drag, after_tax_return as _after_tax_return,
    WHT_BY_ACCOUNT,
)


# =============================================================================
# Account Types
# =============================================================================

class AccountType(Enum):
    """Registered and non-registered account types for asset location."""
    NON_REG = "non_reg"
    TFSA = "tfsa"
    RRSP = "rrsp"
    FHSA = "fhsa"
    RESP = "resp"


# =============================================================================
# Account Composition
# =============================================================================

@dataclass
class YieldBreakdown:
    """Per-account yield breakdown by income type.
    
    DP#27: Each income type has a distinct tax treatment.
    The simulator accepts composition as data and applies the correct
    tax treatment per type per jurisdiction.
    
    All yields are annual rates (e.g., 0.015 = 1.5%).
    """
    eligible_dividends: float = 0.0       # Canadian eligible dividends
    non_eligible_dividends: float = 0.0   # Canadian non-eligible dividends
    interest: float = 0.0                  # Interest income (bonds, GICs)
    capital_gains: float = 0.0             # Realized capital gains
    return_of_capital: float = 0.0         # ROC distributions (reduce ACB)
    foreign_income: float = 0.0            # Foreign dividends/interest

    @property
    def total_yield(self) -> float:
        """Total yield across all income types."""
        return (self.eligible_dividends + self.non_eligible_dividends +
                self.interest + self.capital_gains + self.return_of_capital +
                self.foreign_income)
    
    def validate(self) -> List[str]:
        """Validate yield breakdown. Returns list of warnings."""
        warnings = []
        total = self.total_yield
        if total > 0.15:  # More than 15% total yield is unusual
            warnings.append(f"Total yield {total:.1%} is very high — verify data")
        return warnings


def compute_investment_income(
    balance: float,
    yield_data: Dict[str, float] = None,
    default_yield_rate: float = 0.02,
) -> Dict[str, float]:
    """Compute investment income by type from a portfolio balance.
    
    DP#27: Investment income types have distinct tax treatments.
    Decomposes total returns into component types for correct taxation.
    
    DP#2: Configuration belongs in input, not in code. When yield_data
    is provided (from SimulationConfig.portfolio_data), the composition
    comes from user config. When yield_data is absent, the fallback
    uses default_yield_rate (which defaults to SimulationConfig.non_reg_yield_rate).
    
    Args:
        balance: Portfolio balance
        yield_data: Per-income-type yield rates from user config (DP#2)
        default_yield_rate: Fallback flat yield rate when no yield_data
            provided. Should come from SimulationConfig.non_reg_yield_rate,
            not be hardcoded at call sites.
    
    Returns:
        Dict with {eligible_dividends, interest, total_investment_income}
    """
    if yield_data:
        eligible_div = balance * yield_data.get('eligible_dividends', 0)
        non_eligible_div = balance * yield_data.get('non_eligible_dividends', 0)
        interest = balance * yield_data.get('interest', 0)
        cg = balance * yield_data.get('capital_gains', 0)
        foreign = balance * yield_data.get('foreign_income', 0)
    else:
        # DP#2/DP#13: Fallback uses configurable rate, not hardcoded 2%.
        # Callers should pass default_yield_rate from
        # SimulationConfig.non_reg_yield_rate for accurate results.
        eligible_div = balance * default_yield_rate if balance > 0 else 0
        non_eligible_div = 0.0
        interest = 0.0
        cg = 0.0
        foreign = 0.0
    
    total_investment_income = eligible_div + non_eligible_div + interest + foreign
    
    return {
        'eligible_dividends': eligible_div,
        'non_eligible_dividends': non_eligible_div,
        'interest': interest,
        'capital_gains': cg,
        'foreign_income': foreign,
        'total_investment_income': total_investment_income,
    }


@dataclass
class CompositionBreakdown:
    """Per-account asset allocation breakdown.
    
    Used for asset location optimization. DP#30: composition is user-provided
    data; the simulator computes tax consequences.
    
    All percentages are decimals (e.g., 0.3 = 30%).
    """
    cdn_equity_pct: float = 0.0     # Canadian equities (eligible dividends)
    us_equity_pct: float = 0.0      # US equities (foreign income)
    intl_equity_pct: float = 0.0    # International equities (foreign income)
    fixed_income_pct: float = 0.0   # Bonds/GICs (interest income)

    @property
    def total_pct(self) -> float:
        """Total allocation percentage. Should sum to 1.0 (100%)."""
        return (self.cdn_equity_pct + self.us_equity_pct +
                self.intl_equity_pct + self.fixed_income_pct)
    
    def validate(self) -> List[str]:
        """Validate composition. Returns list of warnings/errors."""
        errors = []
        if abs(self.total_pct - 1.0) > 0.01:
            errors.append(f"Composition sums to {self.total_pct:.0%}, not 100%")
        if any(p < 0 for p in [self.cdn_equity_pct, self.us_equity_pct,
                                self.intl_equity_pct, self.fixed_income_pct]):
            errors.append("Composition percentages cannot be negative")
        return errors

    def income_type_weights(self) -> Dict[IncomeType, float]:
        """Map composition percentages to income type weights.
        
        Canadian equity → eligible dividends (some capital gains)
        US equity → foreign income (eligible for treaty in RRSP)
        International equity → foreign income
        Fixed income → interest
        """
        return {
            IncomeType.ELIGIBLE_DIVIDEND: self.cdn_equity_pct * 0.5,  # ~50% of CDN equity is dividends
            IncomeType.CAPITAL_GAIN: self.cdn_equity_pct * 0.5 +     # CDN growth component
                                     self.us_equity_pct * 0.15 +     # US equity growth
                                     self.intl_equity_pct * 0.15,    # Intl equity growth
            IncomeType.FOREIGN_INCOME: self.us_equity_pct * 0.85 +   # US dividends
                                       self.intl_equity_pct * 0.85,  # Intl dividends
            IncomeType.INTEREST: self.fixed_income_pct,                # Bond interest
            IncomeType.NON_ELIGIBLE_DIVIDEND: 0.0,   # Not common in typical portfolios
            IncomeType.RETURN_OF_CAPITAL: 0.0,         # Derived from specific ETFs
        }


@dataclass
class AccountPortfolio:
    """Per-account portfolio with composition, yield, and ACB tracking.
    
    DP#19: Track cost basis from day one; compute tax at withdrawal.
    DP#16: Auto-include when balance > 0 (empty/zero = disabled).
    """
    balance: float = 0.0
    cost_basis: float = 0.0           # ACB for non-reg; equals balance for registered
    composition: CompositionBreakdown = field(default_factory=CompositionBreakdown)
    yield_breakdown: YieldBreakdown = field(default_factory=YieldBreakdown)

    @property
    def unrealized_gains(self) -> float:
        """Unrealized capital gains (balance - cost_basis)."""
        return max(0, self.balance - self.cost_basis)

    @property
    def has_data(self) -> bool:
        """DP#16: Auto-include when trigger data present."""
        return self.balance > 0 or self.composition.total_pct > 0

    def after_tax_return(self, marginal_rate: float, province: str = 'quebec') -> float:
        """Compute after-tax return for this account based on its yield breakdown.

        Uses per-income-type effective tax rates from income_type module.
        DP#30: Models tax consequences, not investment decisions.

        DP#3/DP#32 (#575): a *rate* is a property of composition and the
        marginal rate, not of the current balance. An account whose balance
        happens to be 0 (the ordinary starting condition for a new taxable
        investor) still has a real after-tax rate -- it just has nothing yet
        to apply it to. This method therefore never branches on ``self.balance``;
        an account with an empty ``yield_breakdown`` naturally returns 0.0
        through the sum below, not through a balance guard.

        Args:
            marginal_rate: Combined marginal tax rate
            province: Province for DTC calculation

        Returns:
            After-tax return rate (e.g., 0.054 = 5.4%)
        """
        total_after_tax = 0.0
        yb = self.yield_breakdown
        
        # Interest: fully taxable at MTR
        if yb.interest > 0:
            eff_rate = effective_tax_rate(IncomeType.INTEREST, marginal_rate, province)
            total_after_tax += yb.interest * (1 - eff_rate)
        
        # Eligible dividends: gross-up + DTC
        if yb.eligible_dividends > 0:
            eff_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, marginal_rate, province)
            total_after_tax += yb.eligible_dividends * (1 - eff_rate)
        
        # Non-eligible dividends: lower DTC
        if yb.non_eligible_dividends > 0:
            eff_rate = effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, marginal_rate, province)
            total_after_tax += yb.non_eligible_dividends * (1 - eff_rate)
        
        # Capital gains: tiered inclusion (DP#27)
        if yb.capital_gains > 0:
            eff_rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, marginal_rate, province)
            total_after_tax += yb.capital_gains * (1 - eff_rate)
        
        # Return of capital: not taxed (reduces ACB instead)
        if yb.return_of_capital > 0:
            total_after_tax += yb.return_of_capital  # Tax-free
        
        # Foreign income: taxable + WHT
        account = 'non_reg'  # Default; should be overridden by method
        if yb.foreign_income > 0:
            eff_rate = effective_tax_rate(IncomeType.FOREIGN_INCOME, marginal_rate, province, account)
            total_after_tax += yb.foreign_income * (1 - eff_rate)
        
        return total_after_tax

    def after_tax_return_by_account(self, account_type: str, marginal_rate: float,
                                     province: str = 'quebec') -> float:
        """Compute after-tax return considering account-level tax treatment.
        
        Registered accounts (RRSP, TFSA, FHSA) shelter all income from tax.
        Non-reg accounts owe tax on each income type.
        
        Args:
            account_type: 'non_reg', 'tfsa', 'rrsp', or 'fhsa'
            marginal_rate: Marginal tax rate (for non-reg and RRSP withdrawal)
            province: Province code
        
        Returns:
            After-tax return rate
        """
        # DP#3/DP#32 (#575): no balance guard here either -- see after_tax_return()
        # above. A rate must not depend on how much money is currently invested.

        # TFSA and FHSA: all income tax-free
        if account_type in ('tfsa', 'fhsa'):
            return self.yield_breakdown.total_yield
        
        # RRSP: tax-deferred growth, taxed on withdrawal
        # For comparison purposes, compute after-tax assuming withdrawal at MTR
        if account_type == 'rrsp':
            gross_return = self.yield_breakdown.total_yield
            return gross_return * (1 - marginal_rate)  # Approximate
        
        # Non-reg: each income type taxed differently
        yb = self.yield_breakdown
        total_after_tax = 0.0
        
        if yb.interest > 0:
            eff = effective_tax_rate(IncomeType.INTEREST, marginal_rate, province, account_type)
            total_after_tax += yb.interest * (1 - eff)
        
        if yb.eligible_dividends > 0:
            eff = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, marginal_rate, province, account_type)
            total_after_tax += yb.eligible_dividends * (1 - eff)
        
        if yb.non_eligible_dividends > 0:
            eff = effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, marginal_rate, province, account_type)
            total_after_tax += yb.non_eligible_dividends * (1 - eff)
        
        if yb.capital_gains > 0:
            eff = effective_tax_rate(IncomeType.CAPITAL_GAIN, marginal_rate, province, account_type)
            # Capital gains are deferred — only taxed when realized
            # Discount for deferral: effective rate lower than statutory
            total_after_tax += yb.capital_gains  # Tax-deferred growth
        
        if yb.return_of_capital > 0:
            total_after_tax += yb.return_of_capital  # Not taxed
        
        if yb.foreign_income > 0:
            eff = effective_tax_rate(IncomeType.FOREIGN_INCOME, marginal_rate, province, account_type)
            total_after_tax += yb.foreign_income * (1 - eff)
        
        return total_after_tax

    def wht_drag_bps(self, account_type: str) -> float:
        """Compute foreign withholding tax drag in basis points.

        Args:
            account_type: 'rrsp', 'tfsa', 'fhsa', 'lira', 'lif', or 'non_reg'

        Returns:
            WHT drag in basis points
        """
        yb = self.yield_breakdown
        foreign_yield = yb.foreign_income
        if foreign_yield <= 0:
            return 0.0

        # WHT recoverability depends on account type. Issue #912 maps FHSA/LIRA/
        # LIF onto the two existing regimes rather than adding new physics (DP#9,
        # one WHT model): an FHSA is a tax-free account with NO US-treaty
        # exemption, so its foreign holdings leak exactly like a TFSA's; a LIRA/
        # LIF is a locked-in RETIREMENT account that DOES carry the RRSP
        # US-treaty exemption, so it leaks exactly like an RRSP's.
        # TFSA/FHSA: not recoverable → positive drag (no US treaty exemption).
        # RRSP/LIRA/LIF (US): treaty exemption → 0 drag for US, intl WHT applies.
        # Non-reg: recoverable via FTC.
        if account_type in ('tfsa', 'fhsa'):
            regime = 'tfsa'
            recoverable = False
        elif account_type in ('rrsp', 'lira', 'lif'):
            regime = 'rrsp'
            recoverable = False
        else:
            regime = 'non_reg'
            recoverable = True
        us_drag = wht_drag(regime, 'us', dividend_yield=foreign_yield * 0.6, wht_recoverable=recoverable)
        intl_drag = wht_drag(regime, 'intl', dividend_yield=foreign_yield * 0.4, wht_recoverable=recoverable)
        return us_drag + intl_drag


# =============================================================================
# Holdings-by-product derivation (issue #547)
# =============================================================================

def _derive_from_holdings(holdings, registry) -> Tuple[CompositionBreakdown, YieldBreakdown]:
    """Derive an account's composition + yield from product-weighted holdings.

    Each holding is ``{'product': name, 'weight': pct}``; weights default to an
    equal split. The result is the weighted sum of each product's composition
    and yield from the registry (DP#27: yields stay split by income type).
    """
    default_weight = 1.0 / len(holdings) if holdings else 0.0
    composition = CompositionBreakdown()
    yield_breakdown = YieldBreakdown()
    for holding in holdings:
        product = registry.get(holding['product'])
        weight = holding.get('weight', default_weight)
        pc, py = product.composition, product.yield_breakdown
        composition.cdn_equity_pct += pc.cdn_equity_pct * weight
        composition.us_equity_pct += pc.us_equity_pct * weight
        composition.intl_equity_pct += pc.intl_equity_pct * weight
        composition.fixed_income_pct += pc.fixed_income_pct * weight
        yield_breakdown.eligible_dividends += py.eligible_dividends * weight
        yield_breakdown.non_eligible_dividends += py.non_eligible_dividends * weight
        yield_breakdown.interest += py.interest * weight
        yield_breakdown.capital_gains += py.capital_gains * weight
        yield_breakdown.return_of_capital += py.return_of_capital * weight
        yield_breakdown.foreign_income += py.foreign_income * weight
    return composition, yield_breakdown


# =============================================================================
# Portfolio Configuration
# =============================================================================

@dataclass
class PortfolioConfig:
    """Complete portfolio configuration across all accounts.
    
    DP#8: compose through data. PortfolioConfig is a data object
    passed to the simulation engine and asset location optimizer.
    
    DP#16: Auto-include when any account has composition data
    or balance > 0. Empty portfolio = disabled.
    """
    allocation_strategy: str = "balanced"
    accounts: Dict[str, AccountPortfolio] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> 'PortfolioConfig':
        """Create PortfolioConfig from input.json portfolio section.

        DP#14: Scripts read a common config schema.
        DP#16: Auto-include when trigger data present.

        An account may declare ``holdings`` by product name instead of raw
        ``composition``/``yield`` buckets (issue #547); the engine then derives
        composition + yield from the product registry. Bucket-only inputs are
        unchanged.
        """
        if not data:
            return cls()

        registry = None
        accounts = {}
        accounts_data = data.get('accounts', {})
        for acct_name, acct_data in accounts_data.items():
            holdings = acct_data.get('holdings')
            if holdings:
                if registry is None:
                    from countries.canada.product_registry import ProductRegistry
                    registry = ProductRegistry.from_dict(data.get('products'))
                composition, yield_breakdown = _derive_from_holdings(holdings, registry)
            else:
                comp_data = acct_data.get('composition', {})
                yield_data = acct_data.get('yield', {})
                composition = CompositionBreakdown(
                    cdn_equity_pct=comp_data.get('cdn_equity_pct', 0),
                    us_equity_pct=comp_data.get('us_equity_pct', 0),
                    intl_equity_pct=comp_data.get('intl_equity_pct', 0),
                    fixed_income_pct=comp_data.get('fixed_income_pct', 0),
                )
                yield_breakdown = YieldBreakdown(
                    eligible_dividends=yield_data.get('eligible_dividends', 0),
                    non_eligible_dividends=yield_data.get('non_eligible_dividends', 0),
                    interest=yield_data.get('interest', 0),
                    capital_gains=yield_data.get('capital_gains', 0),
                    return_of_capital=yield_data.get('return_of_capital', 0),
                    foreign_income=yield_data.get('foreign_income', 0),
                )
            accounts[acct_name] = AccountPortfolio(
                balance=acct_data.get('balance', 0),
                cost_basis=acct_data.get('cost_basis', acct_data.get('balance', 0)),
                composition=composition,
                yield_breakdown=yield_breakdown,
            )
        
        return cls(
            allocation_strategy=data.get('allocation_strategy', 'balanced'),
            accounts=accounts,
        )

    def to_dict(self) -> dict:
        """Export to dict format matching input.json schema. DP#24."""
        accounts_dict = {}
        for name, acct in self.accounts.items():
            accounts_dict[name] = {
                'balance': acct.balance,
                'cost_basis': acct.cost_basis,
                'composition': {
                    'cdn_equity_pct': acct.composition.cdn_equity_pct,
                    'us_equity_pct': acct.composition.us_equity_pct,
                    'intl_equity_pct': acct.composition.intl_equity_pct,
                    'fixed_income_pct': acct.composition.fixed_income_pct,
                },
                'yield': {
                    'eligible_dividends': acct.yield_breakdown.eligible_dividends,
                    'non_eligible_dividends': acct.yield_breakdown.non_eligible_dividends,
                    'interest': acct.yield_breakdown.interest,
                    'capital_gains': acct.yield_breakdown.capital_gains,
                    'return_of_capital': acct.yield_breakdown.return_of_capital,
                    'foreign_income': acct.yield_breakdown.foreign_income,
                },
            }
        return {
            'allocation_strategy': self.allocation_strategy,
            'accounts': accounts_dict,
        }

    @property
    def has_data(self) -> bool:
        """DP#16: Auto-include when any account has data."""
        return any(acct.has_data for acct in self.accounts.values())

    @property
    def total_balance(self) -> float:
        """Total across all accounts."""
        return sum(acct.balance for acct in self.accounts.values())

    def after_tax_return_for_account(self, account_type: str,
                                      marginal_rate: float,
                                      province: str = 'quebec') -> float:
        """Compute after-tax return for a specific account type.
        
        Args:
            account_type: 'non_reg', 'tfsa', 'rrsp', or 'fhsa'
            marginal_rate: Marginal tax rate
            province: Province code
        
        Returns:
            After-tax return rate for the account
        """
        # DP#32 (#575): absence of an account entry is the only legitimate
        # reason to report 0 here -- not a balance of 0 on an account that
        # does exist (a rate is not a function of balance).
        acct = self.accounts.get(account_type)
        if acct is None:
            return 0.0
        return acct.after_tax_return_by_account(account_type, marginal_rate, province)

    def registered_wht_drag(self) -> Dict[str, float]:
        """Issue #641 (extended by #912): the annual foreign-withholding-tax
        drag on each REGISTERED pot, derived from that account's OWN declared
        holdings.

        A registered account (rrsp/tfsa/fhsa/lira/lif) shelters interest and
        dividends from income tax, so its composition does not change its growth
        rate the way a non-reg account's does -- with ONE exception:
        unrecoverable foreign withholding tax. Foreign dividends held in a TFSA
        (or FHSA -- no US-treaty exemption either) lose 15% at source with no
        recovery; US equity in an RRSP (or a locked-in LIRA/LIF, which carry the
        same retirement-account treaty exemption) is treaty-exempt (0%), but
        non-US foreign equity still leaks one level of WHT
        (``income_type.WHT_BY_ACCOUNT``). That leak IS the canonical
        asset-location result #641 exists to make expressible, and it is the
        only tax term that reaches a sheltered account's compounding.

        Returns ``{kind: drag_rate}`` (a decimal rate, e.g. 0.003 = 30 bps) for
        every rrsp/tfsa/fhsa/lira/lif account that declares foreign holdings.
        ``non_reg`` is excluded: its WHT is recoverable via the foreign tax
        credit and its composition already reaches the engine through
        ``non_reg_after_tax_return`` -- counting it here would double-apply it.
        An account with no foreign holdings contributes no entry, so absence is
        a strict no-op (the flat gross rate is preserved -- golden invariant,
        DP#32). The drag reuses ``AccountPortfolio.wht_drag_bps`` (bps -> rate),
        the same physics ``asset_location.py`` scores against (DP#9: one WHT
        model), which maps fhsa->tfsa and lira/lif->rrsp regimes.
        """
        drag: Dict[str, float] = {}
        for kind in ('rrsp', 'tfsa', 'fhsa', 'lira', 'lif'):
            acct = self.accounts.get(kind)
            if acct is None:
                continue
            rate = acct.wht_drag_bps(kind) / 10000.0
            if rate > 0:
                drag[kind] = rate
        return drag

    def optimal_location_analysis(self, marginal_rate: float,
                                    province: str = 'quebec') -> Dict:
        """Analyze asset location optimization across all accounts.
        
        DP#30: Models tax consequences of user's allocation, not investment advice.
        
        Returns:
            Dict with per-account after-tax return and tax drag analysis
        """
        result = {}
        for acct_name, acct in self.accounts.items():
            result[acct_name] = {
                'balance': acct.balance,
                'gross_return': acct.yield_breakdown.total_yield,
                'after_tax_return': acct.after_tax_return_by_account(acct_name, marginal_rate, province),
                'wht_drag_bps': acct.wht_drag_bps(acct_name),
                'composition': acct.composition.total_pct,
            }
        return result


# =============================================================================
# Asset Location Recommendation Engine
# =============================================================================

def asset_location_recommendation(composition: CompositionBreakdown,
                                   marginal_rate: float,
                                   province: str = 'quebec') -> Dict:
    """Recommend optimal account placement for each asset class.
    
    DP#30: The simulator models tax consequences, not investment decisions.
    This function analyzes the after-tax return differential between account
    types for each asset class — it does not choose what to invest in.
    
    Based on SCENARIO_SEED §9.2 (Asset Location Tax Efficiency Matrix):
    - RRSP: Hold interest-bearing + US-listed equity ETFs (avoid WHT)
    - TFSA: Hold Canadian equities + growth assets (no WHT on CDN)
    - Non-reg: Hold Canadian dividend stocks (DTC) + capital-gains-oriented (50% inclusion)
    
    Args:
        composition: Asset allocation percentages
        marginal_rate: Marginal tax rate
        province: Province code
    
    Returns:
        Dict with optimal placement for each asset class
    """
    recommendations = {
        'cdn_equity': {
            'primary': 'non_reg',
            'reason': 'Canadian eligible dividends get DTC in non-reg (net ~25% effective vs 45.7% on interest)',
            'secondary': 'tfsa',
            'avoid': 'rrsp',  # Wastes DTC advantage
            'effective_rate_non_reg': effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, marginal_rate, province, 'non_reg'),
        },
        'us_equity': {
            'primary': 'rrsp',
            'reason': 'US-listed ETFs in RRSP avoid 15% withholding tax (treaty exemption)',
            'secondary': 'tfsa',
            'avoid': None,  # TFSA loses WHT, non-reg recovers via FTC
            'wht_drag_tfsa_bps': wht_drag('tfsa', 'us', dividend_yield=0.02),
            'wht_drag_rrsp_bps': wht_drag('rrsp', 'us', dividend_yield=0.02),
        },
        'intl_equity': {
            'primary': 'rrsp',
            'reason': 'One level of WHT in RRSP vs two levels elsewhere',
            'secondary': 'tfsa',
            'avoid': None,
            'wht_drag_tfsa_bps': wht_drag('tfsa', 'intl', dividend_yield=0.02),
            'wht_drag_rrsp_bps': wht_drag('rrsp', 'intl', dividend_yield=0.02),
        },
        'fixed_income': {
            'primary': 'rrsp',
            'reason': 'Interest fully taxable in non-reg at MTR — shelter in RRSP/TFSA',
            'secondary': 'tfsa',
            'avoid': 'non_reg',  # Highest tax drag
            'effective_rate_non_reg': effective_tax_rate(IncomeType.INTEREST, marginal_rate, province, 'non_reg'),
        },
    }
    
    return recommendations


# Need Dict import at the top but it's available
from typing import Dict