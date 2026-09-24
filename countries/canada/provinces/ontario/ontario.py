#!/usr/bin/env python3
"""Ontario provincial tax data module.

Provides Ontario-specific tax brackets, credits, and rules
as data, not code branches.

Reachability: the ``OntarioTaxData`` container is a data provider (DP#12)
and is unreachable from the simulation fold BY DESIGN — the tax path loads
Ontario brackets from the data files, not through this class. Expected in
the #710 reach guard's allowlist, not dead code (#746).

Sources:
- Ontario 2025/2026 tax brackets
- Ontario doesn't have the federal abatement (only Quebec does)
- Ontario surtax applies above thresholds (on basic Ontario tax, not income)
- Ontario Health Premium (separate levy based on taxable income)
- Ontario Sales Tax Credit (a component of the Ontario Trillium Benefit)
- Ontario Low-income Individuals and Families Tax (LIFT) credit

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Ontario Provincial Tax entry
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
    Surtax thresholds: https://www.taxtips.ca/taxrates/on.htm
    Health Premium: https://www.ontario.ca/page/health-premium
    Ontario Trillium Benefit / OSTC: https://www.canada.ca/en/revenue-agency/services/child-family-benefits/provincial-territorial-programs/province-ontario.html
    LIFT credit: https://www.ontario.ca/page/low-income-workers-tax-credit
"""

from tax_data import TaxYearData, TaxBracket


class OntarioTaxData:
    """Ontario provincial tax data provider."""

    PROVINCE = "ontario"
    ABATEMENT = 0.0  # No abatement for Ontario
    BASIC_PERSONAL_AMOUNT_2026 = 11865
    BASIC_PERSONAL_AMOUNT_2025 = 11863

    # ── Ontario Health Premium tiers (DP#8: structure as data) ──
    # Tuple = (lower_taxable_income, base_premium, marginal_rate, max_premium).
    # Premium = min(max_premium, base + rate × (income − lower)); income at or
    # below $20,000 pays $0. Structure is stable across recent years.
    # Source: https://www.ontario.ca/page/health-premium (O. Reg. — Income Tax Act s.2.2)
    HEALTH_PREMIUM_TIERS = [
        (20000.0, 0.0, 0.06, 300.0),     # >$20,000–$36,000: 6% of income over $20k, max $300
        (36000.0, 300.0, 0.06, 450.0),   # >$36,000–$48,000: $300 + 6% over $36k, max $450
        (48000.0, 450.0, 0.25, 600.0),     # >$48,000–$72,000: $450 + 25% over $48k, max $600
        (72000.0, 600.0, 0.25, 750.0),     # >$72,000–$200,600: $600 + 25% over $72k, max $750
        (200600.0, 750.0, 0.25, 900.0),    # >$200,600: $750 + 25% over $200,600, max $900
    ]

    @classmethod
    def all_years(cls) -> list:
        """Return all known year data for Ontario."""
        return [cls.year_2026(), cls.year_2025()]

    @classmethod
    def year_2026(cls) -> TaxYearData:
        return TaxYearData(
            year=2026, country="canada", province="ontario",
            provincial_brackets=[
                TaxBracket(0, 51446, 0.0505, "5.05%"),
                TaxBracket(51446, 102894, 0.0915, "9.15%"),
                TaxBracket(102894, 150000, 0.1116, "11.16%"),
                TaxBracket(150000, 220000, 0.1216, "12.16%"),
                TaxBracket(220000, 0, 0.1316, "13.16%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=cls.BASIC_PERSONAL_AMOUNT_2026,
            # DP#20: Year-parameterized CPP/OAS data (federal amounts, same for all provinces)
            cpp_max_pensionable=74600,
            cpp_rate=0.0595,
            cpp2_max_pensionable=81900,  # DP#20: CPP2 YAMPE 2026
            cpp2_rate=0.04,
            cpp_max_benefit_65=18092,
            qpp_rate=0.0,  # Ontario doesn't use QPP (DP#52)
            oas_annual_max=8908,
            oas_clawback_threshold=95323,
            # DP#20: Year-versioned provincial DTC rates
            provincial_eligible_dtc_rate=0.1008,     # 10.08% ON DTC on grossed-up eligible
            provincial_non_eligible_dtc_rate=0.0455,  # 4.55% ON DTC on grossed-up non-eligible
            # ── Ontario surtax (2026) ──
            # Source: https://www.taxtips.ca/taxrates/on.htm
            on_surtax_threshold_1=5818,
            on_surtax_rate_1=0.20,
            on_surtax_threshold_2=7446,
            on_surtax_rate_2=0.36,
            # ── Ontario Health Premium ──
            on_health_premium_tiers=cls.HEALTH_PREMIUM_TIERS,
            # ── Ontario Sales Tax Credit (2025 tax year → Jul 2026–Jun 2027) ──
            # Source: https://www.canada.ca/.../province-ontario.html
            on_ostc_amount_per_person=378,
            on_ostc_single_threshold=29047,
            on_ostc_family_threshold=36309,
            on_ostc_reduction_rate=0.04,
            # ── Ontario LIFT credit (fixed since 2022 enhancement) ──
            # Source: https://www.ontario.ca/page/low-income-workers-tax-credit
            on_lift_max=875,
            on_lift_rate=0.0505,
            on_lift_individual_threshold=32500,
            on_lift_family_threshold=65000,
            on_lift_reduction_rate=0.05,
            source="fallback",
        )

    @classmethod
    def year_2025(cls) -> TaxYearData:
        return TaxYearData(
            year=2025, country="canada", province="ontario",
            provincial_brackets=[
                TaxBracket(0, 51346, 0.0505, "5.05%"),
                TaxBracket(51346, 102694, 0.0915, "9.15%"),
                TaxBracket(102694, 150000, 0.1116, "11.16%"),
                TaxBracket(150000, 220000, 0.1216, "12.16%"),
                TaxBracket(220000, 0, 0.1316, "13.16%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=cls.BASIC_PERSONAL_AMOUNT_2025,
            cpp_max_pensionable=68500,
            cpp_rate=0.0595,
            cpp2_max_pensionable=81200,  # DP#20: CPP2 YAMPE 2025
            cpp2_rate=0.04,
            cpp_max_benefit_65=14448,
            qpp_rate=0.0,  # Ontario doesn't use QPP (DP#52)
            oas_annual_max=8381,
            oas_clawback_threshold=90997,
            provincial_eligible_dtc_rate=0.1008,
            provincial_non_eligible_dtc_rate=0.0455,
            # ── Ontario surtax (2025) ──
            # Source: https://www.taxtips.ca/taxrates/on.htm
            on_surtax_threshold_1=5710,
            on_surtax_rate_1=0.20,
            on_surtax_threshold_2=7307,
            on_surtax_rate_2=0.36,
            # ── Ontario Health Premium (structure stable across years) ──
            on_health_premium_tiers=cls.HEALTH_PREMIUM_TIERS,
            # ── Ontario Sales Tax Credit (2024 tax year → Jul 2025–Jun 2026) ──
            # Source: https://www.canada.ca/.../province-ontario.html
            on_ostc_amount_per_person=371,
            on_ostc_single_threshold=28506,
            on_ostc_family_threshold=35632,
            on_ostc_reduction_rate=0.04,
            # ── Ontario LIFT credit (fixed since 2022 enhancement) ──
            on_lift_max=875,
            on_lift_rate=0.0505,
            on_lift_individual_threshold=32500,
            on_lift_family_threshold=65000,
            on_lift_reduction_rate=0.05,
            source="fallback",
        )