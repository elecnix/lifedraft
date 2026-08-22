#!/usr/bin/env python3
"""Canada country tax module.

Registers Canadian federal and provincial tax data with TaxDataProvider.
Each province is a separate data module under countries/canada/provinces/.

# Modules will be re-exported here as they are moved in subsequent phases

Sources:
- CRA income tax brackets by year
- CRA RRSP dollar limits
- CRA TFSA contribution room
- CPP maximum pensionable earnings (YMPE) and rates

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Federal Tax Brackets entry
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/questions-answers-about-contributing-rrsp.html
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html"""

from tax_data import (
    TaxYearData, TaxBracket,
    register_oas_fallback, register_fallback_builder,
)

# ── Push Canada bracket fallbacks into the data layer (DP#25, issue #240) ──
# Register the bracket-fallback builder *before* importing province/retirement
# modules: those modules construct a TaxDataProvider at import time, whose
# auto-registration may re-enter this half-initialized package and fall back
# to TaxDataProvider._build_hardcoded_fallbacks(). That path replays the
# builders registered here, so it must be available early.
# tax_bracket_fallbacks only depends on tax_data, so importing it here is safe.
from countries.canada.tax_bracket_fallbacks import build_fallback_data
register_fallback_builder(build_fallback_data)

# Re-export province modules
from countries.canada.provinces.quebec import (
    QuebecTaxData,
    QuebecDeductionTracker,
    compute_sm_qc_benefit,
    quebec_interest_deduction,
    quebec_sm_portfolio_optimization,
)
from countries.canada.provinces.ontario import OntarioTaxData
from countries.canada.provinces.ontario_credits import (
    ontario_surtax,
    ontario_health_premium,
    ontario_sales_tax_credit,
    ontario_trillium_benefit,
    ontario_lift_credit,
)

# ── Phase 1 leaf modules ────────────────────────────────
# Bank of Canada rate data
from countries.canada.boc_data import (  # noqa: F401
    BoCDataProvider, RateObservation, RateForecast,
    get_current_rates, FALLBACK_PRIME_RATE, FALLBACK_OVERNIGHT_RATE,
)

# Income-type tax treatment
from countries.canada.income_type import (  # noqa: F401
    IncomeType, effective_tax_rate, capital_gains_inclusion_rate,
    wht_drag, after_tax_return,
    CAPITAL_GAINS_INCLUSION, FEDERAL_ELIGIBLE_GROSS_UP,
    FEDERAL_ELIGIBLE_DTC_RATE, FEDERAL_NON_ELIGIBLE_GROSS_UP,
    FEDERAL_NON_ELIGIBLE_DTC_RATE, QC_ELIGIBLE_DTC_RATE,
    QC_NON_ELIGIBLE_DTC_RATE, WHT_BY_ACCOUNT,
)

# IRD / breakage penalty
from countries.canada.ird_penalty import (  # noqa: F401
    compute_ird_penalty, compute_breakage_penalty,
    compute_three_months_interest, refinance_with_penalty_analysis,
    break_for_readvanceable_analysis, DEFAULT_POSTED_RATES,
)

# Mortgage renewal model
from countries.canada.renewal_model import (  # noqa: F401
    MortgageTerm, RenewalEvent, RenewalPathResult,
    simulate_renewal_path, compare_rate_term_options,
    rate_sensitivity_analysis,
)

# FHSA rules (Bill C-47)
from countries.canada.fhsa import (  # noqa: F401
    FHSAAccount, fhsa_double_deduction_analysis,
    FHSA_ANNUAL_LIMIT, FHSA_LIFETIME_LIMIT,
    FHSA_CARRY_FORWARD_MAX, FHSA_MAX_AGE, FHSA_MAX_YEARS_OPEN,
    fhsa_excess_contribution_tax, fhsa_designated_transfer_to_rrsp,
)

# HBP rules (ITA s.146.4)
from countries.canada.hbp_rules import (  # noqa: F401
    HBPAccount, compare_first_home_strategies,
    HBP_MAX_WITHDRAWAL, HBP_REPAYMENT_YEARS,
    HBP_REPAYMENT_START_DELAY, HBP_ANNUAL_MIN_REPAYMENT_PCT,
    HBP_MIN_CONTRIBUTION_DAYS, HBP_RELIEF_START_DELAY, HBP_RELIEF_YEARS,
    repayment_start_delay_for_year, deductible_contribution_before_hbp,
)

# RRSP contribution ledger: the canonical per-contribution ledger lives in
# simulation_state.RRSPListLedger (jurisdiction-agnostic, DP#25). The dead
# countries.canada.rrsp_ledger clone was removed (#744, DP#9).

# Canada-specific simulation state (DP#9)
from countries.canada.sim_state import (  # noqa: F401
    CanadaSimState,
)

# Debt instruments & deductibility (ITA s.20(1)(c))
from countries.canada.debt import (  # noqa: F401
    DebtPurpose, AdvanceRecord, DispositionRecord, DebtInstrument, HELOCTracing,
    PrescribedRateLoan, debt_swap_analysis, cash_dam_analysis,
    is_interest_deductible,
)

# Attribution & TOSI rules (ITA s.74.1, s.74.2, s.104.2)
from countries.canada.attribution import (  # noqa: F401
    TransferType, RecipientRole, IncomeType as AttributionIncomeType,
    TOSIExclusion, AttributionResult,
    check_attribution, check_tosi, attribution_planning_summary,
)

# ── Phase 2 mid-level modules ──────────────────────────

# Account models (RRSP, TFSA, RESP, NonReg)
from countries.canada.account_models import (  # noqa: F401
    RRSPAccount, TSFAccount, RESPAccount, NonRegAccount,
)

# RESP rules (CESG, QESI, CLB)
from countries.canada.resp_rules import (  # noqa: F401
    RESPChild, RESPCalculator, analyze_resp_for_family, print_resp_report,
    CESG_THRESHOLDS, QESI_THRESHOLDS, CLB_THRESHOLDS,
    CESG_ANNUAL_ROOM_CHANGE_YEAR, CESG_CONTRIBUTION_MAX_CHANGE_YEAR,
    get_cesg_thresholds, get_qesi_thresholds, get_clb_thresholds,
    get_cesg_annual_room, get_cesg_contribution_max,
)

# Rate term modeling (mortgage amortization, rate paths, readvanceable mortgage)
from countries.canada.rate_model import (  # noqa: F401
    RateStep, RatePath, HELOCPath, ReadvanceableMortgage,
    build_rate_path, build_broker_scenarios, build_variable_rate_path,
    build_stress_scenarios, build_renewal_stress, build_boc_rate_path_scenario,
    monthly_payment, amortization_schedule, annual_summary,
    estimate_ird_penalty, generate_all_mortgage_scenarios,
    print_amortization_tables,
)

# Market rates provider
from countries.canada.market_rates import (  # noqa: F401
    MortgageRateQuote, MarketRates, MarketRatesProvider,
    FALLBACK_PRIME_RATE, FALLBACK_OVERNIGHT_RATE,
)

# Retirement projections (OAS, CPP, RRIF)
from countries.canada.retirement import (  # noqa: F401
    DrawdownOrder, MemberRetirementData, RetirementState, DrawdownOptimizer,
    oas_clawback, cpp_benefit, rrif_minimum_withdrawal, pension_splitting_available,
    project_retirement,
    PensionIncomeType,  # DP#54: income type verification for pension splitting
    OAS_ANNUAL_MAX, OAS_CLAWBACK_THRESHOLD, OAS_CLAWBACK_RATE,  # DP#20 deprecated: use get_oas_* functions
    get_oas_annual_max, get_oas_annual_max_75plus, get_oas_clawback_threshold,  # DP#20 year-versioned
    CPP_MAX_PENSIONABLE, CPP_EARLY_START_PENALTY, CPP_LATE_START_BONUS,
    RRIF_MIN_WITHDRAWAL_RATES,
    _get_rrif_rates,
    # DP#20/DP#12 (issue #330): year-versioned getters — prefer these over constants
    get_cpp_max_pensionable, get_cpp_max_benefit_65,
    get_gis_max_single, get_gis_max_coupled,
)

# CPP estimator from contributory earnings history (issue #365)
from countries.canada.cpp_estimator import (  # noqa: F401
    EarningsEntry, CPPBenefitEstimate, compute_benefit_estimate,
)

# Pension split optimizer (ITA s.60.03)
from countries.canada.pension_split_optimizer import (  # noqa: F401
    PensionSplitResult, optimize_pension_split, project_pension_split_retirement,
    pension_income_credit, both_spouses_get_credit,
    PENSION_INCOME_CREDIT_MAX, PENSION_CREDIT_RATE,
)

# CPP sharing optimization
from countries.canada.cpp_sharing import (  # noqa: F401
    Province, CPPSharingInput, CPPSharingResult,
    cpp_sharing_eligibility, compute_sharing_ratio, compute_shared_benefits,
    cpp_sharing_tax_benefit, optimize_cpp_sharing,
    combined_cpp_and_pension_split, project_cpp_sharing,
    compute_cpp2_contribution, compute_cpp2_benefit, compute_survivor_benefit,
    CPP_EARLIEST_START_AGE, CPP_STANDARD_AGE, CPP_LATEST_START_AGE,
    CPP_MAX_PENSIONABLE_2026, CPP_MAX_BENEFIT_65_2026,
    CPP2_MAX_PENSIONABLE_2026, CPP_BASIC_EXEMPTION,
    CPP_EARLY_PENALTY_PER_MONTH, CPP_LATE_BONUS_PER_MONTH,
    QPP_MAX_PENSIONABLE_2026, QPP_MAX_BENEFIT_65_2026,
    CPP_RATE_2026, QPP_RATE_2026, CPP2_RATE, CPP2_SELF_EMPLOYED_RATE,
    CPP2_ACCRUAL_RATE,
    CPP_SURVIVOR_RATE_65_PLUS, CPP_SURVIVOR_RATE_UNDER_65, QPP_SURVIVOR_RATE,
    cpp_basic_exemption_pensionable,  # issue #86: consolidated CPP/QPP home
)

# Asset location optimizer
from countries.canada.asset_location import (  # noqa: F401
    AccountType as AssetAccountType, ETFType, PortfolioHolding, AccountAllocation,
    AssetLocationResult, AssetLocationOptimizer,
    compute_tax_drag, light_vs_ludicrous, portfolio_from_config,
)

# Portfolio composition
from countries.canada.portfolio import (  # noqa: F401
    AccountType as PortfolioAccountType, YieldBreakdown, CompositionBreakdown,
    AccountPortfolio, PortfolioConfig,
    compute_investment_income, asset_location_recommendation,
)

# Cash-out optimizer (readvanceable mortgage extraction)
from countries.canada.cashout_optimizer import (  # noqa: F401
    AccountNeed, CashOutPlan,
    compute_per_dollar_benefit, compute_tfsa_per_dollar,
    compute_nonreg_per_dollar, compute_paydown_per_dollar,
    compute_min_extraction, print_cashout_report,
)

# ── Phase 5: Canadian tax calculation convenience functions ────────────
from countries.canada.tax_calc import (  # noqa: F401
    QC_ABATEMENT,
    federal_tax, federal_tax_before_abatement, quebec_abatement_amount,
    combined_tax_separate, quebec_tax,
    rrsp_deduction_savings, rrsp_deduct_later_savings,
    spousal_rrsp_benefit,
    tax_on_eligible_dividend, effective_dividend_rate,
    withholding_tax_drag, asset_location_suggestion, asset_location_tax_impact,
    canada_employment_credit, basic_personal_amount_credit,
    tuition_tax_credit,
    optimize_bpa_transfer, compute_non_refundable_credits,
    compute_total_tax,
)

# ── Phase 6: Alternative Minimum Tax (AMT) ──────────────────────────
from countries.canada.amt import (  # noqa: F401
    AMTParameters, compute_amt, amt_adjusted_income, total_tax_with_amt,
    AMTCredit, carry_forward_amt_credit,
    QuebecIMRParameters, compute_quebec_imr,
)

# LSIF credit rules (ITA s.127.4, TA s.1029.8.5)
from countries.canada.lsif_credit import (  # noqa: F401
    LSIFPurchase, LSIFCreditResult, compute_lsif_credit, lsif_from_config,
    holding_period_years, qc_highest_provincial_bracket_threshold,
    FEDERAL_LSIF_RATE, QUEBEC_LSIF_RATE, LSIF_PURCHASE_MAX,
)

# Issue #826 (DP#7/#10/#12): Fonds FTQ product rules -- locked-until-65
# illiquidity + the sourced 7.3% 10-year-CAR expected return, encoded in the
# module so a contract account only needs product='fonds_ftq' (no restated
# rules). The LSIF credit itself stays in lsif_credit.py (re-exported through
# fonds_ftq for one program entry point).
from countries.canada.fonds_ftq import (  # noqa: F401
    ProductRules, ftq_product_rules, resolve_product,
    FTQ_10Y_CAR, FTQ_UNLOCK_AGE,
)

# ── Phase 7: Canadian allocation strategies ────────────────────────
from countries.canada.strategies import (  # noqa: F401
    STRATEGY_BALANCED, STRATEGY_RRSP_MAX,
    STRATEGY_READVANCE_PRIORITY, STRATEGY_NO_READVANCE,
    STRATEGIES, discover_strategies,
)

# Canada-specific simulation state dataclasses (DP#9, DP#25, issue #25).
# These were moved from simulation_state.py to countries/canada/sim_state.py
# because they are Canada-specific, not jurisdiction-agnostic.
from countries.canada.sim_state import (  # noqa: F401
    HelocTracingState, QcDeductionState,
)

# Quebec tax brackets proxy (DP#25)
from tax_calculator import (  # noqa: F401
    QUEBEC_TAX_BRACKETS_2026,
)

PROVINCES = {
    "quebec": QuebecTaxData,
    "ontario": OntarioTaxData,
    "qc": QuebecTaxData,  # alias
    "on": OntarioTaxData,  # alias
}

# ── Push Canada OAS fallback data into the data layer (DP#25, issue #240) ──
# The data layer (tax_data) must not import countries.canada. Instead, this
# package — when imported (DP#16 package-presence trigger) — registers its
# OAS-by-year amounts into TaxDataProvider, so the dependency points inward
# (countries.canada → tax_data), never outward. retirement is fully imported
# by this point (re-exported above), so CPP_OAS_BY_YEAR is available.
from countries.canada.retirement import CPP_OAS_BY_YEAR
register_oas_fallback(CPP_OAS_BY_YEAR)

# ── Push Canada allocation strategies into the core strategy engine (DP#25, ──
# ── issue #284) ──────────────────────────────────────────────────────────
# The core strategy module (strategy.py) must not import countries.canada.
# Instead, this package — when imported (DP#16 package-presence trigger) —
# registers its strategies into strategy._STRATEGY_REGISTRY, so the dependency
# points inward (countries.canada → strategy), never outward. strategies is
# fully imported by this point (re-exported in the Phase 7 block above), so
# its register() is available. strategy.list_strategies() triggers country
# discovery (importing this package) rather than importing Canada directly.
from countries.canada.strategies import register as _register_strategies
_register_strategies()

# ── Push Canada LIF-conversion provider into the simulation layer (DP#25, ──
# ── issue #283) ────────────────────────────────────────────────────────────
# The simulation layer (simulation_state.py) must not import
# countries.canada.locked_in_account. Instead, this package — when imported
# (DP#16 package-presence trigger) — registers its locked-in/LIF conversion
# provider into simulation_state, so the dependency points inward
# (countries.canada → simulation_state), never outward. The provider forwards
# to LockedInAccount/LIFFund/must_convert_by_year, so simulate_year_pure's
# LIRA→LIF conversion (incl. issue #343's calendar-year gate) is unchanged.
from simulation_state import register_lif_conversion_provider
from countries.canada.locked_in_account import LIF_CONVERSION_PROVIDER
register_lif_conversion_provider(LIF_CONVERSION_PROVIDER)


def register(tax_provider):
    """Register all Canadian tax data with a TaxDataProvider instance."""
    for province_code, province_module in PROVINCES.items():
        for year_data in province_module.all_years():
            # Register under the canonical province key (year_data.province).
            tax_provider.register_year(year_data)
            # Also register under the postal-code alias (e.g. 'qc' -> 'quebec')
            # so lookups by either form resolve and project forward (issue #295).
            # Without this, a config using province='qc' never matched the
            # 'quebec' keys, fell through to the freeze-with-warning path, and
            # so its brackets were never indexed beyond the base year.
            if province_code != year_data.province:
                tax_provider.register_year_alias(province_code, year_data)

    # Also register federal-only data (for provinces we haven't added yet)
    for year_data in federal_all_years():
        key = f"canada:federal:{year_data.year}"
        if key not in tax_provider._fallbacks:
            tax_provider.register_year(year_data)

    # Register Canadian strategies with the core strategy engine
    from countries.canada.strategies import register as _register_strategies
    _register_strategies()


# Issue #635 (DP#9): federal_all_years now lives in the province-independent
# countries.canada.federal_tax_data module so the fallback builder can import
# it during countries.canada's re-entrant import (before this __init__ is
# fully defined). Re-exported here so existing `from countries.canada import
# federal_all_years` callers are unchanged.
from countries.canada.federal_tax_data import federal_all_years  # noqa: F401
