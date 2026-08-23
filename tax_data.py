#!/usr/bin/env python3
"""Tax data provider — year-specific brackets, limits, and rates.

Tax brackets are data, not code. This module provides them by year,
loaded from cached government sources or built-in fallbacks.

Country/province modules under `countries/` register their data
with the provider. Adding a new province = adding a data module,
not changing code.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Federal/Provincial Tax Brackets entries
    Federal brackets: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
    CPP contribution rates / YMPE: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html
    MP/DB/RRSP/DPSP limits and YMPE by year:
        https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html
    TFSA dollar limits by year:
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html
    Quebec provincial rates: https://www.revenuquebec.ca/en/individuals/income-tax-rates/
    Ontario provincial rates: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html

Usage:
    from tax_data import TaxDataProvider
    provider = TaxDataProvider()
    brackets_2026_qc = provider.get_brackets(year=2026, country='canada', province='quebec')
    brackets_2026_on = provider.get_brackets(year=2026, country='canada', province='ontario')
    brackets_2025_qc = provider.get_brackets(year=2025, country='canada', province='quebec')
"""

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# ── Jurisdiction fallback registry (DP#25/DP#10, issue #240) ─────────────
# The data layer (this module) must not import jurisdiction code such as
# `countries.canada`. Instead, country packages push their fallback data
# into this registry at import time (DP#16 package-presence trigger).
#
# `tax_data` defines the interface; `countries/canada/__init__.py` calls
# register_oas_fallback() / register_fallback_builder() when imported, so
# the dependency points inward (countries → core), not outward.
_OAS_FALLBACK_BY_YEAR: Dict[int, dict] = {}
_FALLBACK_BUILDERS: List[Callable[[], list]] = []


def register_oas_fallback(by_year: Dict[int, dict]) -> None:
    """Register year-keyed OAS fallback amounts from a jurisdiction module.

    Called by country packages (e.g. countries.canada) at import time so the
    data layer never imports jurisdiction code (DP#25/DP#10, issue #240).
    """
    _OAS_FALLBACK_BY_YEAR.update(by_year)


def register_fallback_builder(builder: Callable[[], list]) -> None:
    """Register a builder of TaxYearData fallbacks from a jurisdiction module.

    Called by country packages at import time. ``_build_hardcoded_fallbacks``
    invokes every registered builder, so the data layer never imports
    jurisdiction bracket data directly (DP#25/DP#10, issue #240).
    """
    if builder not in _FALLBACK_BUILDERS:
        _FALLBACK_BUILDERS.append(builder)

# ── CRA RRIF minimum withdrawal rate fallback (DP#2/DP#13, issue #86) ────
# Per DP#13: these are round-number approximations of CRA-prescribed factors.
# The exact values come from ITA Reg. 7301 and CRA RC4167.
# Ages 55-64: 1/(90-age) per CRA RC4167, rounded per industry standard
# Ages 65+: CRA prescribed factor table
# These are USED ONLY as fallbacks when TaxDataProvider doesn't have
# year-specific data. The canonical source is TaxDataProvider.
_CRA_RRIF_FALLBACK_RATES: Dict[int, float] = {
    55: 0.0286,  # 1/(90-55) = 0.028571
    56: 0.0294,  # 1/(90-56) = 0.029412
    57: 0.0303,  # 1/(90-57) = 0.030303
    58: 0.0313,  # 1/(90-58) = 0.031250 → 0.0313 per industry standard
    59: 0.0323,  # 1/(90-59) = 0.032258
    60: 0.0333,  # 1/(90-60) = 0.033333
    61: 0.0345,  # 1/(90-61) = 0.034483
    62: 0.0357,  # 1/(90-62) = 0.035714
    63: 0.0370,  # 1/(90-63) = 0.037037
    64: 0.0385,  # 1/(90-64) = 0.038462
    65: 0.0400,
    66: 0.0417,
    67: 0.0435,
    68: 0.0455,
    69: 0.0476,
    70: 0.0500,
    71: 0.0528,  # Mandatory RRIF conversion age
    72: 0.0540,
    73: 0.0553,
    74: 0.0567,
    75: 0.0582,
    76: 0.0598,
    77: 0.0615,
    78: 0.0633,
    79: 0.0652,
    80: 0.0672,
    81: 0.0693,
    82: 0.0716,
    83: 0.0740,
    84: 0.0764,
    85: 0.0790,
    86: 0.0817,
    87: 0.0846,
    88: 0.0876,
    89: 0.0908,
    90: 0.0940,
    91: 0.0974,
    92: 0.1009,
    93: 0.1047,
    94: 0.1087,
    95: 0.1129,
}


@dataclass
class TaxBracket:
    """A single tax bracket with min/max income and rate."""
    min_income: float
    max_income: float  # 0 means unlimited
    rate: float
    label: str = ""


@dataclass
class TaxYearData:
    """All tax data for a specific year + jurisdiction."""
    year: int
    country: str
    province: str
    federal_brackets: List[TaxBracket] = field(default_factory=list)
    provincial_brackets: List[TaxBracket] = field(default_factory=list)
    provincial_abatement: float = 0.0  # e.g., Quebec 16.5%
    rrsp_limit: float = 0.0
    tfsa_limit: float = 0.0
    cpp_max_pensionable: float = 0.0
    cpp_rate: float = 0.0
    cpp_exemption: float = 3500.0
    # ── CPP2 (second additional CPP) parameters (DP#20, DP#12) ──
    cpp2_max_pensionable: float = 0.0    # CPP2 YAMPE / YMPE2 (second earnings ceiling)
    cpp2_rate: float = 0.0               # CPP2 contribution rate (4% since 2024)
    cpp_max_benefit_65: float = 0.0  # Max CPP retirement benefit at age 65 (DP#20)
    # ── QPP-specific parameters (DP#52, issue #52) ──
    qpp_rate: float = 0.0                # QPP contribution rate (6.40% for 2026, higher than CPP 5.95%)
    qpp_max_benefit_65: float = 0.0      # Max QPP retirement benefit at age 65
    qpp_survivor_flat_rate: float = 0.0   # QPP survivor flat-rate component (annual)
    oas_annual_max: float = 0.0       # OAS maximum annual amount (DP#20)
    oas_annual_max_75plus: float = 0.0  # OAS annual amount for age 75+ (10% enhancement, DP#20)
    oas_clawback_threshold: float = 0.0  # OAS recovery threshold (DP#20)
    # ── GIS (Guaranteed Income Supplement) maximums (DP#12/DP#20, issue #330) ──
    gis_max_single: float = 0.0          # GIS annual maximum for single pensioner (DP#20)
    gis_max_coupled: float = 0.0         # GIS annual maximum for coupled pensioner (DP#20)
    gis_income_exemption: float = 0.0    # GIS income exemption (first $K ignored, DP#20)
    fhsa_limit: float = 0.0            # FHSA annual contribution limit (DP#20)
    basic_personal_amount: float = 0.0
    bpa_phaseout_threshold: float = 0.0  # Net income where BPA enhancement begins to phase out (CRA year-versioned)
    bpa_phaseout_end: float = 0.0      # Net income where BPA reaches minimum after phaseout (CRA year-versioned)
    bpa_minimum: float = 0.0            # Floor for BPA after phaseout
    canada_employment_amount: float = 0.0   # Canada Employment Amount (line 31260, DP#20)
    # ── Federal dividend tax credit & capital gains parameters (DP#20, DP#27) ──
    federal_eligible_dtc_rate: float = 0.0       # Federal DTC rate on grossed-up eligible dividends
    federal_non_eligible_dtc_rate: float = 0.0   # Federal DTC rate on grossed-up non-eligible dividends
    federal_eligible_gross_up: float = 0.38       # Gross-up rate for eligible dividends (38%)
    federal_non_eligible_gross_up: float = 0.15   # Gross-up rate for non-eligible dividends (15%)
    capital_gains_inclusion_rate: float = 0.50   # Base capital gains inclusion rate
    capital_gains_upper_inclusion_rate: float = 0.0  # Upper tier inclusion rate (0 = flat, not tiered)
    capital_gains_threshold: float = 0.0         # Threshold for upper tier (0 = no tier)
    # ── Provincial dividend tax credit rates (DP#20) ──
    provincial_eligible_dtc_rate: float = 0.0    # Provincial DTC rate on grossed-up eligible dividends
    provincial_non_eligible_dtc_rate: float = 0.0 # Provincial DTC rate on grossed-up non-eligible
    # ── Quebec solidarity tax credit (DP#20) ──
    qc_solidarity_single_max: float = 0.0       # Max annual credit for single person
    qc_solidarity_couple_max: float = 0.0        # Max annual credit for couple
    qc_solidarity_single_threshold: float = 0.0  # Income threshold where reduction starts (single)
    qc_solidarity_couple_threshold: float = 0.0  # Income threshold where reduction starts (couple)
    qc_solidarity_reduction_rate_low: float = 0.03   # 3% reduction rate on income above threshold
    qc_solidarity_reduction_rate_high: float = 0.06  # 6% reduction rate on income above high threshold
    qc_solidarity_high_threshold: float = 0.0       # Income threshold where 6% rate kicks in
    # ── Quebec health services fund (FSS) (DP#20) ──
    qc_fss_self_employed_rate: float = 0.0       # FSS rate for self-employed
    # ── Quebec FSS employer contribution rates by sector (DP#20, issue #323) ──
    # Source: Revenu Québec, Total Payroll Threshold and Health Services Fund
    # Contribution Rate. Rate varies by total payroll and by sector. Below the
    # lower payroll threshold ($1M), primary/manufacturing employers pay the
    # reduced rate; other ("services") employers pay the standard rate. At/above
    # the upper payroll threshold all employers pay the maximum rate.
    qc_fss_employer_rate_max: float = 0.0           # Max FSS employer rate (large payroll)
    qc_fss_employer_rate_primary_min: float = 0.0   # Min rate, primary/manufacturing (small payroll)
    qc_fss_employer_rate_services_min: float = 0.0  # Min rate, other/services (small payroll)
    qc_fss_employer_payroll_lower: float = 0.0      # Lower total-payroll threshold ($1M)
    qc_fss_employer_payroll_upper: float = 0.0      # Upper total-payroll threshold (max rate above)
    # ── Quebec individual contribution to the Health Services Fund (line 446) ──
    # Source: Revenu Québec, Individual Contributions to the Health Services Fund.
    # Two-bracket formula on income excluding employment income/most benefits.
    qc_fss_individual_exemption: float = 0.0     # Income below which no contribution is owed
    qc_fss_individual_first_cap: float = 0.0     # Max contribution in first bracket ($150)
    qc_fss_individual_second_threshold: float = 0.0  # Second-bracket income threshold
    qc_fss_individual_max: float = 0.0           # Max individual contribution ($1,000)
    qc_fss_individual_rate: float = 0.0          # Marginal rate (1%)
    # ── Quebec Parental Insurance Plan (QPIP) (DP#20) ──
    qpip_employee_rate: float = 0.0              # QPIP employee contribution rate
    qpip_employer_rate: float = 0.0             # QPIP employer contribution rate
    qpip_self_employed_rate: float = 0.0        # QPIP self-employed contribution rate
    qpip_max_insurable_earnings: float = 0.0    # QPIP maximum insurable earnings
    # ── Ontario surtax (DP#20, issue #324) ──
    # Ontario levies a surtax on *basic Ontario tax* (after non-refundable
    # credits), not on taxable income. Two tiers stack: 20% above the first
    # threshold and an additional 36% above the second (total 56%).
    on_surtax_threshold_1: float = 0.0   # Basic ON tax above which 20% surtax applies
    on_surtax_rate_1: float = 0.0        # First surtax rate (0.20)
    on_surtax_threshold_2: float = 0.0   # Basic ON tax above which the extra 36% applies
    on_surtax_rate_2: float = 0.0        # Second (additional) surtax rate (0.36)
    # ── Ontario Health Premium (DP#20, issue #324) ──
    # A separate levy based on taxable income (not a surtax on tax). Modeled
    # as a list of (lower_income, base_premium, marginal_rate, max_premium)
    # tiers; the premium is base + rate × (income − lower), capped at max.
    on_health_premium_tiers: List[tuple] = field(default_factory=list)
    # ── Ontario Sales Tax Credit / Trillium component (DP#20, issue #324) ──
    on_ostc_amount_per_person: float = 0.0      # Max OSTC per adult and per child
    on_ostc_single_threshold: float = 0.0       # AFNI threshold (single, no children)
    on_ostc_family_threshold: float = 0.0        # AFNI threshold (family / has children)
    on_ostc_reduction_rate: float = 0.0          # Reduction rate above threshold (4%)
    # ── Ontario LIFT credit (DP#20, issue #324) ──
    on_lift_max: float = 0.0                      # Max LIFT credit
    on_lift_rate: float = 0.0                      # Rate applied to employment income (5.05%)
    on_lift_individual_threshold: float = 0.0     # Adjusted individual net income threshold
    on_lift_family_threshold: float = 0.0          # Adjusted family net income threshold
    on_lift_reduction_rate: float = 0.0            # Reduction rate (5%)
    # ── Quebec non-refundable credit parameters (DP#20) ──
    qc_charitable_donation_threshold: float = 0.0  # Threshold for low/high donation rate split
    qc_charitable_donation_rate_low: float = 0.0    # Rate on donations up to threshold
    qc_charitable_donation_rate_high: float = 0.0   # Rate on donations above threshold
    qc_medical_expense_threshold_pct: float = 0.0  # Medical expense threshold as % of net income
    qc_charitable_donation_rate_top: float = 0.0   # Rate above 3rd-bracket income (25.75%)
    qc_charitable_donation_top_threshold: float = 0.0  # Taxable income where top rate applies
    # ── Quebec senior assistance tax credit (refundable, DP#20, issue #321) ──
    # Source: Revenu Québec, Senior Assistance Tax Credit; Québec Ministère des
    # Finances, Parameters of the Personal Income Tax System.
    qc_senior_assistance_max_per_person: float = 0.0   # Max credit per eligible person 70+
    qc_senior_assistance_threshold_single: float = 0.0 # Family-income reduction threshold (single)
    qc_senior_assistance_threshold_couple: float = 0.0 # Family-income reduction threshold (couple)
    qc_senior_assistance_reduction_rate: float = 0.0   # Reduction rate above threshold
    # ── Quebec amount with respect to age / living alone / retirement (line 361) ──
    # Source: Revenu Québec, Age Amount, Amount for a Person Living Alone and
    # Amount for Retirement Income. These non-refundable amounts share a single
    # family-income reduction threshold and reduction rate.
    qc_age_amount: float = 0.0                 # Amount with respect to age (65+)
    qc_living_alone_amount: float = 0.0        # Basic amount for a person living alone
    qc_retirement_income_amount: float = 0.0   # Amount for retirement income
    qc_age_credit_reduction_threshold: float = 0.0  # Shared family-income reduction threshold
    qc_age_credit_reduction_rate: float = 0.0  # Reduction rate above threshold (18.75%)
    qc_non_refundable_credit_rate: float = 0.0 # Conversion rate for QC non-refundable amounts (14%)
    # ── Quebec tuition tax credit (TP-1 Schedule T, issue #783) ──
    # A SPECIFIC non-refundable credit rate on eligible tuition (8% since
    # 2013), NOT the 14% general qc_non_refundable_credit_rate. Sourced from
    # Revenu Québec Schedule T (TP-1.D.T-V), line 45. Year-versioned (DP#20);
    # 0.0 for non-Quebec jurisdictions (the field is Quebec-specific).
    qc_tuition_credit_rate: float = 0.0
    # ── Quebec work premium (prime au travail, refundable, issue #321) ──
    # Source: Revenu Québec, Work Premium Tax Credits; Québec Ministère des
    # Finances, Parameters of the Personal Income Tax System (general work premium).
    qc_work_premium_max_single: float = 0.0    # Max for person living alone
    qc_work_premium_max_couple: float = 0.0    # Max for couple without children
    qc_work_premium_excluded_single: float = 0.0  # Excluded work income (one adult)
    qc_work_premium_excluded_couple: float = 0.0  # Excluded work income (couple)
    qc_work_premium_growth_rate: float = 0.0   # Growth rate on work income above excluded amount
    qc_work_premium_reduction_threshold_single: float = 0.0  # Reduction threshold (one adult)
    qc_work_premium_reduction_threshold_couple: float = 0.0  # Reduction threshold (couple)
    qc_work_premium_reduction_rate: float = 0.0  # Reduction rate above threshold (10%)
    # ── Quebec prescription drug insurance premium (line 447, issue #321) ──
    # Source: RAMQ, Prescription Drug Insurance — Rates in Effect. The premium
    # is income-tested; this models the maximum annual premium per adult.
    qc_drug_insurance_max_premium: float = 0.0  # Maximum annual premium per adult
    # ── RESP/CESG/QESI/CLB thresholds (DP#12, DP#20) ──
    # CESG (Canada Education Savings Grant) income thresholds
    cesg_first_threshold: float = 0.0       # Lower CESG income threshold
    cesg_second_threshold: float = 0.0      # Upper CESG income threshold
    # QESI (Quebec Education Savings Incentive) income thresholds
    qesi_first_threshold: float = 0.0      # Lower QESI income threshold
    qesi_second_threshold: float = 0.0     # Upper QESI income threshold
    # CLB (Canada Learning Bond) income thresholds by family size
    clb_threshold_1_3_children: float = 0.0   # CLB threshold for 1-3 children
    clb_threshold_4_children: float = 0.0     # CLB threshold for 4 children
    clb_threshold_5plus_children: float = 0.0 # CLB threshold for 5+ children
    # ── RRIF minimum withdrawal rates (DP#2/DP#12/DP#20, issue #86) ──
    # CRA-prescribed RRIF minimum withdrawal factors, indexed by year.
    # Key: age (int), Value: minimum withdrawal fraction (float)
    # If empty, falls back to the built-in CRA factors.
    rrif_min_withdrawal_rates: Dict[int, float] = field(default_factory=dict)
    source: str = ""  # "gov", "cache", "fallback"


class TaxDataProvider:
    """Provides tax brackets and limits by year, country, and province.

    Data sources (in priority order):
    1. User-provided config (exact values)
    2. Local cache (if fresh)
    3. Country/province modules (auto-discovered from countries/ package)
    4. Built-in fallbacks for recent years

    This module does NOT hardcode tax brackets. It provides them
    dynamically so that:
    - Same code works for 2025, 2026, and future years
    - Provincial differences are data, not code branches
    - Bracket indexation is handled by updating data, not changing code
    """

    def __init__(self, cache_dir: str = None, auto_register: bool = True, registry=None):
        self.cache_dir = cache_dir or os.path.expanduser(
            "~/.cache/lifedraft/tax"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self._fallbacks: Dict[str, TaxYearData] = {}
        # Memoized _load_year results (issue #295 hot path). A given calendar
        # year's projected brackets/limits are INVARIANT across every optimizer
        # scenario and every year-step of every fold, yet _load_year was
        # re-projecting them from the base year on ~1M calls per optimize run
        # (~99s of a ~78s→ profile). We cache the resolved TaxYearData keyed by
        # (country, province, year, indexation_rate). The rate is part of the
        # key because _project_from_base reads self.indexation_rate, so a
        # provider whose rate is changed after warmup (only test paths do this)
        # must not serve a stale projection. register_year* invalidate the
        # cache, since new base data can change projections.
        self._year_cache: Dict[Tuple[str, str, int, float], TaxYearData] = {}
        # Annual indexation rate used to project bracket thresholds and limits
        # forward beyond the latest year of real data (issue #295). CRA indexes
        # brackets ~annually by the CPI factor; we mirror that with geometric
        # growth. Defaults to 2%; callers wire this to assumptions.inflation.
        self.indexation_rate: float = 0.02

        # True once the country registration ran against the full `countries`
        # registry. Flipped to False when we fall back to the hardcoded
        # builders — which happens in a countries-less deployment OR,
        # critically, when this constructor runs RE-ENTRANTLY during a partial
        # (circular) import of the countries package: a module-level caller
        # builds a provider before `countries` has finished importing, so
        # `register_all` isn't importable yet and only the fallback builders
        # registered so far exist. The instance is then degraded (e.g. missing
        # provinces/aliases). default_tax_provider() reads this flag and refuses
        # to memoize a degraded build, which is what poisoned the process-wide
        # cache (issue #840).
        self._registration_complete = True

        if auto_register:
            if registry is not None:
                registry.register_all(self)
            else:
                try:
                    from countries import register_all
                    register_all(self)
                except ImportError:
                    # No countries package (e.g. isolated test), or called
                    # re-entrantly during a partial import of it (issue #840).
                    self._registration_complete = False
                    self._build_hardcoded_fallbacks()

    def register_year(self, data: TaxYearData):
        """Register a TaxYearData with the provider.

        Country/province modules call this to make their data available.
        """
        key = f"{data.country}:{data.province}:{data.year}"
        self._fallbacks[key] = data
        # New base data can change what projections resolve to; drop memoized
        # year results so they are recomputed against the updated table.
        self._year_cache.clear()

    def register_year_alias(self, province_alias: str, data: TaxYearData):
        """Register year data under an alternate province key (e.g. postal code).

        Country modules use this so that a config referring to a province by
        its postal code ('qc') resolves to the same data registered under the
        canonical name ('quebec'). The data layer stays jurisdiction-agnostic:
        it just stores the alias key the country module asks for.
        """
        key = f"{data.country}:{province_alias}:{data.year}"
        self._fallbacks[key] = data
        self._year_cache.clear()

    def get_brackets(self, year: int, country: str = "canada",
                     province: str = "quebec",
                     combined: bool = True) -> List[TaxBracket]:
        """Get tax brackets for a specific year and jurisdiction.

        Args:
            year: Taxation year (e.g., 2026)
            country: Country code
            province: Province code
            combined: If True, return combined federal+provincial brackets

        Returns:
            List of TaxBracket objects, sorted by min_income
        """
        data = self._load_year(year, country, province)

        if combined and data.federal_brackets and data.provincial_brackets:
            return self._combine_brackets(
                data.federal_brackets,
                data.provincial_brackets,
                data.provincial_abatement,
            )
        if combined and data.provincial_brackets and not data.federal_brackets:
            # Province-only data (need to load federal separately)
            fed_data = self._load_year(year, country, "federal")
            return self._combine_brackets(
                fed_data.federal_brackets,
                data.provincial_brackets,
                data.provincial_abatement,
            )
        if combined and data.federal_brackets and not data.provincial_brackets:
            # Federal-only data (need to load province separately)
            prov_data = self._load_year(year, country, province)
            if prov_data.provincial_brackets:
                return self._combine_brackets(
                    data.federal_brackets,
                    prov_data.provincial_brackets,
                    prov_data.provincial_abatement,
                )
            # No provincial data at all — return federal only
            return list(data.federal_brackets)

        return data.federal_brackets + data.provincial_brackets

    def get_combined_brackets(self, year: int = 2026,
                               province: str = "quebec") -> List[dict]:
        """Get combined federal+provincial brackets as a list of dicts.

        Dict format ({min, max, rate, label}) consumed by the pure tax
        functions in ``tax_calculator`` (marginal_rate, tax_on_income, …).
        Callers hold a ``TaxDataProvider`` explicitly (DP#9): there is no
        module-level convenience wrapper.
        """
        brackets = self.get_brackets(year, province=province, combined=True)
        return [
            {"min": b.min_income, "max": b.max_income, "rate": b.rate,
             "label": b.label}
            for b in brackets
        ]

    def get_split_brackets(self, year: int = 2026,
                           province: str = "quebec") -> tuple:
        """The combined bracket list SPLIT into its two jurisdiction slices.

        Returns ``(federal_slice, provincial_slice)`` in the same dict format
        ``get_combined_brackets`` returns. The federal slice carries the
        provincial-abatement reduction (exactly as ``_combine_brackets``
        applies it), so valuing amount A federally and amount B provincially
        sums to the combined valuation of ``A + B`` when the slices' amounts
        partition it — which is what lets a deduction with a FEDERAL-only
        limit be priced without a blended rate (issue #1035: s.20(1)(c)
        investment interest has no federal investment-income limit; only TA
        s.336.0.1 caps the Quebec slice).

        Raises ValueError when no tax data exists for the year/province — the
        same loud failure ``get_combined_brackets`` produces (DP#32).
        """
        data = self._load_year(year, "canada", province)
        federal_source = data.federal_brackets
        if not federal_source:
            federal_source = self._load_year(
                year, "canada", "federal").federal_brackets
        abatement = data.provincial_abatement
        # Scale the federal brackets by the abatement exactly as
        # _combine_brackets does, so fed-slice + prov-slice reproduces the
        # combined list's piecewise rates when summed (DP#9: one spelling).
        federal_scaled = [
            TaxBracket(min_income=b.min_income, max_income=b.max_income,
                       rate=round(b.rate * (1 - abatement), 4), label=b.label)
            for b in federal_source]

        def _fmt(merged):
            return [{"min": b.min_income, "max": b.max_income, "rate": b.rate,
                     "label": b.label} for b in merged]

        return (_fmt(self._merge_overlapping(federal_scaled)),
                _fmt(self._merge_overlapping(list(data.provincial_brackets))))

    def get_rrsp_limit(self, year: int, country: str = "canada") -> float:
        """Get the RRSP dollar limit for a year."""
        data = self._load_year(year, country, "federal")
        return data.rrsp_limit

    def get_tfsa_limit(self, year: int, country: str = "canada") -> float:
        """Get the TFSA dollar limit for a year."""
        data = self._load_year(year, country, "federal")
        return data.tfsa_limit

    def get_fhsa_limit(self, year: int, country: str = "canada") -> float:
        """Get the FHSA annual contribution limit for a year (DP#20).
        
        The FHSA limit has been $8,000 since its introduction in 2023.
        Per DP#20, this is year-versioned so future changes don't require code edits.
        """
        data = self._load_year(year, country, "federal")
        if data.fhsa_limit > 0:
            return data.fhsa_limit
        # Fallback: FHSA introduced in 2023 at $8,000 (DP#13: clearly round default)
        if year >= 2023:
            return 8000
        return 0  # FHSA didn't exist before 2023

    def get_cpp2_max_pensionable(self, year: int, country: str = "canada") -> float:
        """Get the CPP2 YAMPE (Yearly Additional Maximum Pensionable Earnings).
        
        Per DP#20, CPP2 max pensionable is year-versioned data from tax_data.py,
        not a hardcoded constant.
        
        CPP2 was introduced in 2024 with a second earnings ceiling (YMPE2)
        above the regular YMPE. For 2023, CPP2 existed but YMPE2 = YMPE.
        
        Sources:
            https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
        """
        data = self._load_year(year, country, "federal")
        if data.cpp2_max_pensionable > 0:
            return data.cpp2_max_pensionable
        # Fallback: CPP2 didn't exist before 2024 in its current form
        return 0

    def get_cpp_max_pensionable(self, year: int, country: str = "canada") -> float:
        """Get the CPP YMPE (Year's Maximum Pensionable Earnings) for a year (DP#12/DP#20, issue #330).
        
        Per DP#12, government-published data does not belong as hardcoded constants
        in library modules. Per DP#20, CPP max pensionable is year-versioned data.
        This replaces the module-level CPP_MAX_PENSIONABLE constant.
        
        Sources:
            https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html
        """
        data = self._load_year(year, country, "federal")
        if data.cpp_max_pensionable > 0:
            return data.cpp_max_pensionable
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year].get("cpp_max_pensionable", 0)
        return 68500  # DP#13: 2024 fallback

    def get_cpp_max_benefit_65(self, year: int, country: str = "canada") -> float:
        """Get the maximum CPP retirement benefit at age 65 for a year (DP#12/DP#20, issue #330).
        
        Per DP#12 and DP#20, CPP max benefit is year-versioned data from the data
        provider, not a hardcoded constant. This replaces the module-level
        CPP_MAX_BENEFIT_65 constant.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit-amounts.html
        """
        data = self._load_year(year, country, "federal")
        if data.cpp_max_benefit_65 > 0:
            return data.cpp_max_benefit_65
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year].get("cpp_max_benefit_65", 0)
        return 14448  # DP#13: 2024 fallback

    def get_gis_max_single(self, year: int, country: str = "canada") -> float:
        """Get the GIS annual maximum for a single pensioner (DP#12/DP#20, issue #330).
        
        Per DP#12 and DP#20, GIS amounts are year-versioned data from the data
        provider, not hardcoded constants. This replaces the module-level
        GIS_ANNUAL_MAX_SINGLE constant.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/guaranteed-income-supplement.html
        """
        data = self._load_year(year, country, "federal")
        if data.gis_max_single > 0:
            return data.gis_max_single
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year].get("gis_max_single", 0)
        return 1616  # DP#13: 2024 fallback

    def get_gis_max_coupled(self, year: int, country: str = "canada") -> float:
        """Get the GIS annual maximum for a coupled pensioner (DP#12/DP#20, issue #330).
        
        Per DP#12 and DP#20, GIS amounts are year-versioned data from the data
        provider, not hardcoded constants. This replaces the module-level
        GIS_ANNUAL_MAX_COUPLED constant.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/guaranteed-income-supplement.html
        """
        data = self._load_year(year, country, "federal")
        if data.gis_max_coupled > 0:
            return data.gis_max_coupled
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year].get("gis_max_coupled", 0)
        return 9739  # DP#13: 2024 fallback

    def get_gis_income_exemption(self, year: int, country: str = "canada") -> float:
        """Get the GIS income exemption amount (DP#12/DP#20, issue #330).
        
        The first $K of non-OAS income is exempt from GIS calculations.
        Per DP#12 and DP#20, this is year-versioned data.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/guaranteed-income-supplement.html
        """
        data = self._load_year(year, country, "federal")
        if data.gis_income_exemption > 0:
            return data.gis_income_exemption
        return 4000  # DP#13: 2026 fallback


    def get_oas_annual_max(self, year: int, country: str = "canada") -> float:
        """Get the OAS maximum annual amount for ages 65-74 (DP#20).
        
        Per DP#20, OAS amounts are year-versioned data from tax_data.py,
        not hardcoded constants. This replaces the module-level
        OAS_ANNUAL_MAX constant.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/oas-amounts.html
        """
        data = self._load_year(year, country, "federal")
        if data.oas_annual_max > 0:
            return data.oas_annual_max
        # Fallback to jurisdiction-registered OAS data (DP#25, issue #240)
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year]["oas_annual_max"]
        # Ultimate fallback: 2026 value (DP#13)
        return 8908

    def get_oas_annual_max_75plus(self, year: int, country: str = "canada") -> float:
        """Get the OAS maximum annual amount for age 75+ (10% enhancement, DP#20).
        
        Since July 2022, OAS recipients aged 75+ receive a 10% enhancement.
        Per DP#20, this amount is year-versioned data from tax_data.py.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/oas-amounts.html
        """
        data = self._load_year(year, country, "federal")
        if data.oas_annual_max_75plus > 0:
            return data.oas_annual_max_75plus
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year]["oas_annual_max_75plus"]
        return 9800  # DP#13: 2026 fallback

    def get_oas_clawback_threshold(self, year: int, country: str = "canada") -> float:
        """Get the OAS clawback (recovery tax) threshold (DP#20).
        
        Per DP#20, the OAS clawback threshold is year-versioned data from
        tax_data.py, not a hardcoded constant. This replaces the module-level
        OAS_CLAWBACK_THRESHOLD constant.
        
        Sources:
            https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/repayment.html
        """
        data = self._load_year(year, country, "federal")
        if data.oas_clawback_threshold > 0:
            return data.oas_clawback_threshold
        if year in _OAS_FALLBACK_BY_YEAR:
            return _OAS_FALLBACK_BY_YEAR[year]["oas_clawback_threshold"]
        return 95323  # DP#13: 2026 fallback

    def available_years(self, country: str = "canada",
                        province: str = None) -> List[int]:
        """List years for which data is available."""
        years = set()
        for key, data in self._fallbacks.items():
            parts = key.split(":")
            if len(parts) < 2:
                continue
            if parts[0] != country:
                continue
            if province is not None and parts[1] != province and parts[1] != "federal":
                continue
            years.add(data.year)
        return sorted(years)

    def get_rrif_min_withdrawal_rates(self, year: int = 2026,
                                          country: str = "canada") -> Dict[int, float]:
        """Get RRIF minimum withdrawal rates for a given year (DP#12, DP#20).

        CRA-prescribed factors may change across tax years. This method
        looks up year-specific data from the TaxDataProvider, falling
        back to the built-in CRA factors if not available.

        Args:
            year: Taxation year (e.g., 2026)
            country: Country code

        Returns:
            Dict mapping age → minimum withdrawal fraction
        """
        data = self._load_year(year, country, "federal")
        if data.rrif_min_withdrawal_rates:
            return data.rrif_min_withdrawal_rates
        # Fallback: built-in CRA factors (2015+ factors, unchanged as of 2026)
        return _CRA_RRIF_FALLBACK_RATES

    def get_year_data(self, year: int, country: str = "canada",
                      province: str = "quebec") -> TaxYearData:
        """Public accessor for year-versioned tax data.

        Same as _load_year but part of the public API.

        Args:
            year: Taxation year
            country: Country code
            province: Province code

        Returns:
            TaxYearData for the requested year and jurisdiction
        """
        return self._load_year(year, country, province)

    def _load_year(self, year: int, country: str,
                   province: str) -> TaxYearData:
        """Load tax data for a year, memoized per (jurisdiction, year, rate).

        The resolved TaxYearData for a calendar year is invariant across every
        optimizer scenario and year-step, so it is computed once via
        ``_load_year_uncached`` and reused (issue #295 hot path). The cache key
        includes ``self.indexation_rate`` because projections depend on it; the
        exact-match branch already returned the shared ``_fallbacks`` object, so
        callers are known to treat results as read-only and sharing the cached
        object is safe.
        """
        cache_key = (country, province, year, self.indexation_rate)
        cached = self._year_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._load_year_uncached(year, country, province)
        self._year_cache[cache_key] = result
        return result

    def _load_year_uncached(self, year: int, country: str,
                            province: str) -> TaxYearData:
        """Resolve tax data for a year. Tries cache → fallback → projection."""
        key = f"{country}:{province}:{year}"

        # Try exact match in fallbacks
        if key in self._fallbacks:
            return self._fallbacks[key]

        # Try cache
        cached = self._load_cache(f"{key}.json")
        if cached is not None:
            return self._parse_cached(cached)

        # Try nearest year and project if beyond (DP#20)
        available = self.available_years(country, province)
        if available:
            nearest = min(available, key=lambda y: abs(y - year))
            nearest_key = f"{country}:{province}:{nearest}"
            if nearest_key in self._fallbacks:
                base_data = self._fallbacks[nearest_key]
                if year > nearest:
                    return self._project_from_base(base_data, year)
                return base_data

        raise ValueError(f"No tax data for {country}/{province}/{year}")
    
    def _project_from_base(self, base: TaxYearData, target_year: int,
                             indexation_rate: float = None) -> TaxYearData:
        """Project future tax data from a base year using indexation.
        
        DP#20: simulate across tax years, not within a single year's brackets.
        Brackets and limits are escalated by an annual indexation factor.
        Rates stay constant (only thresholds shift with inflation).
        
        Args:
            base: Known TaxYearData to project from
            target_year: Year to project to
            indexation_rate: Annual inflation rate for indexation. When None,
                uses self.indexation_rate (wired to assumptions.inflation).

        Returns:
            Projected TaxYearData with escalated brackets/limits
        """
        if indexation_rate is None:
            indexation_rate = self.indexation_rate
        years_ahead = target_year - base.year
        factor = (1 + indexation_rate) ** years_ahead
        
        def escalate_brackets(brackets: List[TaxBracket]) -> List[TaxBracket]:
            return [
                TaxBracket(
                    min_income=round(b.min_income * factor, 2) if b.min_income > 0 else 0,
                    max_income=round(b.max_income * factor, 2) if b.max_income > 0 else 0,
                    rate=b.rate,
                    label=f"{b.label} (projected {target_year})",
                )
                for b in brackets
            ]
        
        return TaxYearData(
            year=target_year,
            country=base.country,
            province=base.province,
            federal_brackets=escalate_brackets(base.federal_brackets),
            provincial_brackets=escalate_brackets(base.provincial_brackets),
            provincial_abatement=base.provincial_abatement,
            rrsp_limit=round(base.rrsp_limit * factor, 2) if base.rrsp_limit else 0,
            tfsa_limit=round(base.tfsa_limit * factor, 2) if base.tfsa_limit else 0,
            cpp_max_pensionable=round(base.cpp_max_pensionable * factor, 2) if base.cpp_max_pensionable else 0,
            cpp_rate=base.cpp_rate,
            cpp_exemption=round(base.cpp_exemption * factor, 2) if base.cpp_exemption else 0,
            cpp2_max_pensionable=round(base.cpp2_max_pensionable * factor, 2) if base.cpp2_max_pensionable else 0,
            cpp2_rate=base.cpp2_rate,
            qpp_rate=base.qpp_rate,
            qpp_max_benefit_65=round(base.qpp_max_benefit_65 * factor, 2) if base.qpp_max_benefit_65 else 0,
            qpp_survivor_flat_rate=round(base.qpp_survivor_flat_rate * factor, 2) if base.qpp_survivor_flat_rate else 0,
            fhsa_limit=round(base.fhsa_limit * factor, 2) if base.fhsa_limit else 0,
            basic_personal_amount=round(base.basic_personal_amount * factor, 2) if base.basic_personal_amount else 0,
            bpa_phaseout_threshold=round(base.bpa_phaseout_threshold * factor, 2) if base.bpa_phaseout_threshold else 0,
            bpa_phaseout_end=round(base.bpa_phaseout_end * factor, 2) if base.bpa_phaseout_end else 0,
            bpa_minimum=round(base.bpa_minimum * factor, 2) if base.bpa_minimum else 0,
            canada_employment_amount=round(base.canada_employment_amount * factor, 2) if base.canada_employment_amount else 0,
            federal_eligible_dtc_rate=base.federal_eligible_dtc_rate,
            federal_non_eligible_dtc_rate=base.federal_non_eligible_dtc_rate,
            federal_eligible_gross_up=base.federal_eligible_gross_up,
            federal_non_eligible_gross_up=base.federal_non_eligible_gross_up,
            capital_gains_inclusion_rate=base.capital_gains_inclusion_rate,
            capital_gains_upper_inclusion_rate=base.capital_gains_upper_inclusion_rate,
            capital_gains_threshold=round(base.capital_gains_threshold * factor, 2) if base.capital_gains_threshold else 0,
            provincial_eligible_dtc_rate=base.provincial_eligible_dtc_rate,
            provincial_non_eligible_dtc_rate=base.provincial_non_eligible_dtc_rate,
            # ── Quebec solidarity credit (escalated) ──
            qc_solidarity_single_max=round(base.qc_solidarity_single_max * factor, 2) if base.qc_solidarity_single_max else 0,
            qc_solidarity_couple_max=round(base.qc_solidarity_couple_max * factor, 2) if base.qc_solidarity_couple_max else 0,
            qc_solidarity_single_threshold=round(base.qc_solidarity_single_threshold * factor, 2) if base.qc_solidarity_single_threshold else 0,
            qc_solidarity_couple_threshold=round(base.qc_solidarity_couple_threshold * factor, 2) if base.qc_solidarity_couple_threshold else 0,
            qc_solidarity_reduction_rate_low=base.qc_solidarity_reduction_rate_low,
            qc_solidarity_reduction_rate_high=base.qc_solidarity_reduction_rate_high,
            qc_solidarity_high_threshold=round(base.qc_solidarity_high_threshold * factor, 2) if base.qc_solidarity_high_threshold else 0,
            # ── Quebec FSS (rates constant; payroll thresholds escalated) ──
            qc_fss_self_employed_rate=base.qc_fss_self_employed_rate,
            qc_fss_employer_rate_max=base.qc_fss_employer_rate_max,
            qc_fss_employer_rate_primary_min=base.qc_fss_employer_rate_primary_min,
            qc_fss_employer_rate_services_min=base.qc_fss_employer_rate_services_min,
            qc_fss_employer_payroll_lower=round(base.qc_fss_employer_payroll_lower * factor, 2) if base.qc_fss_employer_payroll_lower else 0,
            qc_fss_employer_payroll_upper=round(base.qc_fss_employer_payroll_upper * factor, 2) if base.qc_fss_employer_payroll_upper else 0,
            qc_fss_individual_exemption=round(base.qc_fss_individual_exemption * factor, 2) if base.qc_fss_individual_exemption else 0,
            qc_fss_individual_first_cap=base.qc_fss_individual_first_cap,
            qc_fss_individual_second_threshold=round(base.qc_fss_individual_second_threshold * factor, 2) if base.qc_fss_individual_second_threshold else 0,
            qc_fss_individual_max=base.qc_fss_individual_max,
            qc_fss_individual_rate=base.qc_fss_individual_rate,
            # ── Senior assistance / age credit / work premium / drug premium ──
            qc_senior_assistance_max_per_person=base.qc_senior_assistance_max_per_person,
            qc_senior_assistance_threshold_single=round(base.qc_senior_assistance_threshold_single * factor, 2) if base.qc_senior_assistance_threshold_single else 0,
            qc_senior_assistance_threshold_couple=round(base.qc_senior_assistance_threshold_couple * factor, 2) if base.qc_senior_assistance_threshold_couple else 0,
            qc_senior_assistance_reduction_rate=base.qc_senior_assistance_reduction_rate,
            qc_age_amount=round(base.qc_age_amount * factor, 2) if base.qc_age_amount else 0,
            qc_living_alone_amount=round(base.qc_living_alone_amount * factor, 2) if base.qc_living_alone_amount else 0,
            qc_retirement_income_amount=round(base.qc_retirement_income_amount * factor, 2) if base.qc_retirement_income_amount else 0,
            qc_age_credit_reduction_threshold=round(base.qc_age_credit_reduction_threshold * factor, 2) if base.qc_age_credit_reduction_threshold else 0,
            qc_age_credit_reduction_rate=base.qc_age_credit_reduction_rate,
            qc_non_refundable_credit_rate=base.qc_non_refundable_credit_rate,
            qc_tuition_credit_rate=base.qc_tuition_credit_rate,
            qc_work_premium_max_single=round(base.qc_work_premium_max_single * factor, 2) if base.qc_work_premium_max_single else 0,
            qc_work_premium_max_couple=round(base.qc_work_premium_max_couple * factor, 2) if base.qc_work_premium_max_couple else 0,
            qc_work_premium_excluded_single=base.qc_work_premium_excluded_single,
            qc_work_premium_excluded_couple=base.qc_work_premium_excluded_couple,
            qc_work_premium_growth_rate=base.qc_work_premium_growth_rate,
            qc_work_premium_reduction_threshold_single=round(base.qc_work_premium_reduction_threshold_single * factor, 2) if base.qc_work_premium_reduction_threshold_single else 0,
            qc_work_premium_reduction_threshold_couple=round(base.qc_work_premium_reduction_threshold_couple * factor, 2) if base.qc_work_premium_reduction_threshold_couple else 0,
            qc_work_premium_reduction_rate=base.qc_work_premium_reduction_rate,
            qc_drug_insurance_max_premium=round(base.qc_drug_insurance_max_premium * factor, 2) if base.qc_drug_insurance_max_premium else 0,
            # ── QPIP (rates constant, max earnings escalated) ──
            qpip_employee_rate=base.qpip_employee_rate,
            qpip_employer_rate=base.qpip_employer_rate,
            qpip_self_employed_rate=base.qpip_self_employed_rate,
            qpip_max_insurable_earnings=round(base.qpip_max_insurable_earnings * factor, 2) if base.qpip_max_insurable_earnings else 0,
            # ── Quebec non-refundable credit params (rates constant) ──
            qc_charitable_donation_threshold=base.qc_charitable_donation_threshold,  # threshold stays constant
            qc_charitable_donation_rate_low=base.qc_charitable_donation_rate_low,
            qc_charitable_donation_rate_high=base.qc_charitable_donation_rate_high,
            qc_charitable_donation_rate_top=base.qc_charitable_donation_rate_top,
            qc_charitable_donation_top_threshold=round(base.qc_charitable_donation_top_threshold * factor, 2) if base.qc_charitable_donation_top_threshold else 0,
            qc_medical_expense_threshold_pct=base.qc_medical_expense_threshold_pct,
            # ── RESP/CESG/QESI/CLB thresholds (escalated) ──
            cesg_first_threshold=round(base.cesg_first_threshold * factor, 2) if base.cesg_first_threshold else 0,
            cesg_second_threshold=round(base.cesg_second_threshold * factor, 2) if base.cesg_second_threshold else 0,
            qesi_first_threshold=round(base.qesi_first_threshold * factor, 2) if base.qesi_first_threshold else 0,
            qesi_second_threshold=round(base.qesi_second_threshold * factor, 2) if base.qesi_second_threshold else 0,
            clb_threshold_1_3_children=round(base.clb_threshold_1_3_children * factor, 2) if base.clb_threshold_1_3_children else 0,
            clb_threshold_4_children=round(base.clb_threshold_4_children * factor, 2) if base.clb_threshold_4_children else 0,
            clb_threshold_5plus_children=round(base.clb_threshold_5plus_children * factor, 2) if base.clb_threshold_5plus_children else 0,
            source="projected",
        )

    def _combine_brackets(self, federal: List[TaxBracket],
                          provincial: List[TaxBracket],
                          abatement: float) -> List[TaxBracket]:
        """Combine federal and provincial brackets with abatement.

        Federal brackets are reduced by the provincial abatement
        (e.g., Quebec 16.5% abatement reduces federal by 0.835).
        For provinces with no abatement (e.g., Ontario), abatement=0.
        """
        combined = []
        for fb in federal:
            effective_rate = fb.rate * (1 - abatement)
            combined.append(TaxBracket(
                min_income=fb.min_income,
                max_income=fb.max_income,
                rate=round(effective_rate, 4),
                label=f"Fed ({fb.label}) × {1 - abatement:.3f}",
            ))
        for pb in provincial:
            combined.append(TaxBracket(
                min_income=pb.min_income,
                max_income=pb.max_income,
                rate=round(pb.rate, 4),
                label=f"Prov ({pb.label})",
            ))

        combined.sort(key=lambda b: b.min_income)
        return self._merge_overlapping(combined)

    def _merge_overlapping(self, brackets: List[TaxBracket]) -> List[TaxBracket]:
        """Merge brackets into piecewise-constant intervals."""
        if not brackets:
            return []

        # Collect all boundary points
        points = sorted(set(
            [b.min_income for b in brackets] +
            [b.max_income for b in brackets if b.max_income > 0]
        ))

        merged = []
        for i in range(len(points) - 1):
            mid = (points[i] + points[i + 1]) / 2
            total_rate = sum(b.rate for b in brackets
                           if b.min_income <= mid and
                           (b.max_income == 0 or b.max_income > mid))
            merged.append(TaxBracket(
                min_income=points[i],
                max_income=points[i + 1],
                rate=round(total_rate, 4),
            ))

        # Last bracket (max_income == 0)
        if points:
            last = points[-1]
            total_rate = sum(b.rate for b in brackets
                           if b.min_income <= last and b.max_income == 0)
            merged.append(TaxBracket(
                min_income=last,
                max_income=0,
                rate=round(total_rate, 4),
            ))

        return merged

    def _load_cache(self, filename: str) -> Optional[dict]:
        path = os.path.join(self.cache_dir, filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _parse_cached(self, data: dict) -> TaxYearData:
        return TaxYearData(
            year=data.get("year", 0),
            country=data.get("country", ""),
            province=data.get("province", ""),
            federal_brackets=[
                TaxBracket(**b) for b in data.get("federal_brackets", [])
            ],
            provincial_brackets=[
                TaxBracket(**b) for b in data.get("provincial_brackets", [])
            ],
            provincial_abatement=data.get("provincial_abatement", 0),
            rrsp_limit=data.get("rrsp_limit", 0),
            tfsa_limit=data.get("tfsa_limit", 0),
            fhsa_limit=data.get("fhsa_limit", 0),
            federal_eligible_dtc_rate=data.get("federal_eligible_dtc_rate", 0),
            federal_non_eligible_dtc_rate=data.get("federal_non_eligible_dtc_rate", 0),
            federal_eligible_gross_up=data.get("federal_eligible_gross_up", 0.38),
            federal_non_eligible_gross_up=data.get("federal_non_eligible_gross_up", 0.15),
            capital_gains_inclusion_rate=data.get("capital_gains_inclusion_rate", 0.50),
            capital_gains_upper_inclusion_rate=data.get("capital_gains_upper_inclusion_rate", 0),
            capital_gains_threshold=data.get("capital_gains_threshold", 0),
            provincial_eligible_dtc_rate=data.get("provincial_eligible_dtc_rate", 0),
            provincial_non_eligible_dtc_rate=data.get("provincial_non_eligible_dtc_rate", 0),
            basic_personal_amount=data.get("basic_personal_amount", 0),
            bpa_phaseout_threshold=data.get("bpa_phaseout_threshold", 0),
            bpa_phaseout_end=data.get("bpa_phaseout_end", 0),
            bpa_minimum=data.get("bpa_minimum", 0),
            canada_employment_amount=data.get("canada_employment_amount", 0),
            qc_solidarity_single_max=data.get("qc_solidarity_single_max", 0),
            qc_solidarity_couple_max=data.get("qc_solidarity_couple_max", 0),
            qc_solidarity_single_threshold=data.get("qc_solidarity_single_threshold", 0),
            qc_solidarity_couple_threshold=data.get("qc_solidarity_couple_threshold", 0),
            qc_solidarity_reduction_rate_low=data.get("qc_solidarity_reduction_rate_low", 0.03),
            qc_solidarity_reduction_rate_high=data.get("qc_solidarity_reduction_rate_high", 0.06),
            qc_solidarity_high_threshold=data.get("qc_solidarity_high_threshold", 0),
            qc_fss_self_employed_rate=data.get("qc_fss_self_employed_rate", 0),
            qc_fss_employer_rate_max=data.get("qc_fss_employer_rate_max", 0),
            qc_fss_employer_rate_primary_min=data.get("qc_fss_employer_rate_primary_min", 0),
            qc_fss_employer_rate_services_min=data.get("qc_fss_employer_rate_services_min", 0),
            qc_fss_employer_payroll_lower=data.get("qc_fss_employer_payroll_lower", 0),
            qc_fss_employer_payroll_upper=data.get("qc_fss_employer_payroll_upper", 0),
            qc_fss_individual_exemption=data.get("qc_fss_individual_exemption", 0),
            qc_fss_individual_first_cap=data.get("qc_fss_individual_first_cap", 0),
            qc_fss_individual_second_threshold=data.get("qc_fss_individual_second_threshold", 0),
            qc_fss_individual_max=data.get("qc_fss_individual_max", 0),
            qc_fss_individual_rate=data.get("qc_fss_individual_rate", 0),
            qc_senior_assistance_max_per_person=data.get("qc_senior_assistance_max_per_person", 0),
            qc_senior_assistance_threshold_single=data.get("qc_senior_assistance_threshold_single", 0),
            qc_senior_assistance_threshold_couple=data.get("qc_senior_assistance_threshold_couple", 0),
            qc_senior_assistance_reduction_rate=data.get("qc_senior_assistance_reduction_rate", 0),
            qc_age_amount=data.get("qc_age_amount", 0),
            qc_living_alone_amount=data.get("qc_living_alone_amount", 0),
            qc_retirement_income_amount=data.get("qc_retirement_income_amount", 0),
            qc_age_credit_reduction_threshold=data.get("qc_age_credit_reduction_threshold", 0),
            qc_age_credit_reduction_rate=data.get("qc_age_credit_reduction_rate", 0),
            qc_non_refundable_credit_rate=data.get("qc_non_refundable_credit_rate", 0),
            qc_tuition_credit_rate=data.get("qc_tuition_credit_rate", 0),
            qc_work_premium_max_single=data.get("qc_work_premium_max_single", 0),
            qc_work_premium_max_couple=data.get("qc_work_premium_max_couple", 0),
            qc_work_premium_excluded_single=data.get("qc_work_premium_excluded_single", 0),
            qc_work_premium_excluded_couple=data.get("qc_work_premium_excluded_couple", 0),
            qc_work_premium_growth_rate=data.get("qc_work_premium_growth_rate", 0),
            qc_work_premium_reduction_threshold_single=data.get("qc_work_premium_reduction_threshold_single", 0),
            qc_work_premium_reduction_threshold_couple=data.get("qc_work_premium_reduction_threshold_couple", 0),
            qc_work_premium_reduction_rate=data.get("qc_work_premium_reduction_rate", 0),
            qc_drug_insurance_max_premium=data.get("qc_drug_insurance_max_premium", 0),
            qpip_employee_rate=data.get("qpip_employee_rate", 0),
            qpip_employer_rate=data.get("qpip_employer_rate", 0),
            qpip_self_employed_rate=data.get("qpip_self_employed_rate", 0),
            qpip_max_insurable_earnings=data.get("qpip_max_insurable_earnings", 0),
            qc_charitable_donation_threshold=data.get("qc_charitable_donation_threshold", 0),
            qc_charitable_donation_rate_low=data.get("qc_charitable_donation_rate_low", 0),
            qc_charitable_donation_rate_high=data.get("qc_charitable_donation_rate_high", 0),
            qc_charitable_donation_rate_top=data.get("qc_charitable_donation_rate_top", 0),
            qc_charitable_donation_top_threshold=data.get("qc_charitable_donation_top_threshold", 0),
            qc_medical_expense_threshold_pct=data.get("qc_medical_expense_threshold_pct", 0),
            # ── RESP/CESG/QESI/CLB thresholds ──
            cesg_first_threshold=data.get("cesg_first_threshold", 0),
            cesg_second_threshold=data.get("cesg_second_threshold", 0),
            qesi_first_threshold=data.get("qesi_first_threshold", 0),
            qesi_second_threshold=data.get("qesi_second_threshold", 0),
            clb_threshold_1_3_children=data.get("clb_threshold_1_3_children", 0),
            clb_threshold_4_children=data.get("clb_threshold_4_children", 0),
            clb_threshold_5plus_children=data.get("clb_threshold_5plus_children", 0),
            cpp_max_pensionable=data.get("cpp_max_pensionable", 0),
            cpp_rate=data.get("cpp_rate", 0),
            cpp_exemption=data.get("cpp_exemption", 3500),
            cpp2_max_pensionable=data.get("cpp2_max_pensionable", 0),
            cpp2_rate=data.get("cpp2_rate", 0),
            cpp_max_benefit_65=data.get("cpp_max_benefit_65", 0),
            oas_annual_max=data.get("oas_annual_max", 0),
            oas_annual_max_75plus=data.get("oas_annual_max_75plus", 0),
            oas_clawback_threshold=data.get("oas_clawback_threshold", 0),
            gis_max_single=data.get("gis_max_single", 0),
            gis_max_coupled=data.get("gis_max_coupled", 0),
            gis_income_exemption=data.get("gis_income_exemption", 0),
            qpp_rate=data.get("qpp_rate", 0),
            qpp_max_benefit_65=data.get("qpp_max_benefit_65", 0),
            qpp_survivor_flat_rate=data.get("qpp_survivor_flat_rate", 0),
            source=data.get("source", "cache"),
        )

    def _build_hardcoded_fallbacks(self):
        """Minimal fallbacks when country modules aren't auto-registered.

        Real data comes from country/province modules via register_all().
        This path replays builders that jurisdiction packages registered at
        import time via register_fallback_builder() (DP#25, issue #240), so
        the data layer never imports jurisdiction bracket data directly.

        DP#12: Bracket data lives in the registering jurisdiction module
        (e.g. countries.canada.tax_bracket_fallbacks).
        """
        for builder in _FALLBACK_BUILDERS:
            for year_data in builder():
                self.register_year(year_data)


_DEFAULT_PROVIDER: "TaxDataProvider | None" = None
_DEFAULT_PROVIDER_GENERATION: int = -1


def _current_registry_generation() -> int:
    """Read the generation of the default countries registry.

    Lazy-imports ``countries`` to avoid circular imports at module load.
    If the import fails (partial / re-entrant import), returns -1 so the
    caller treats the generation as unchanged.
    """
    try:
        from countries import default_registry
        return default_registry.generation
    except ImportError:
        return -1


def default_tax_provider() -> "TaxDataProvider":
    """Return a process-wide cached ``TaxDataProvider`` with default indexation.

    Constructing a provider runs ``register_all`` — it re-registers every
    country module and rebuilds the static federal/provincial year tables. In a
    single optimizer run that constructor was firing ~224k times (the
    retirement-drawdown inner loops each built a fresh provider per call),
    making module registration ~37% of total CPU.

    The registered base-year data (``_fallbacks``) is identical across every
    provider — indexation is applied at *query* time (``_project_from_base``
    reads ``self.indexation_rate``), not at registration — so read-only callers
    that use the default indexation (0.02) can safely share one instance.
    Callers that set a custom ``indexation_rate`` (only the live simulation,
    via ``simulation.py``) must keep building their own provider.

    The cache is invalidated when the *registry* (the set of country register
    functions) changes — not when a provider absorbs the registry state via
    register_year/register_year_alias. A fresh TaxDataProvider() construction
    does NOT bump the registry generation, so building non-default providers
    does not invalidate this cache.
    """
    global _DEFAULT_PROVIDER, _DEFAULT_PROVIDER_GENERATION
    current_gen = _current_registry_generation()
    if _DEFAULT_PROVIDER is not None and _DEFAULT_PROVIDER_GENERATION == current_gen:
        return _DEFAULT_PROVIDER
    provider = TaxDataProvider()
    if not provider._registration_complete:
        # Built re-entrantly during a partial import of the countries
        # package: registration is degraded (e.g. missing the 'qc' alias or
        # whole provinces). Return it for THIS call but do not memoize, so a
        # later call — after imports finish — caches a fully-registered
        # provider instead of poisoning the cache forever (issue #840).
        return provider
    _DEFAULT_PROVIDER = provider
    _DEFAULT_PROVIDER_GENERATION = _current_registry_generation()
    return _DEFAULT_PROVIDER
