#!/usr/bin/env python3
"""Federal tax-bracket data, extracted to a province-independent module.

Issue #635 (DP#9 single source of truth): the federal fallback
(``countries.canada.tax_bracket_fallbacks.build_fallback_data``) is called
during the re-entrant import of ``countries.canada`` -- *before* the province
modules and the rest of ``countries/canada/__init__.py`` are fully defined.
It therefore cannot import ``federal_all_years`` from ``countries.canada`` at
that moment (the name is not bound yet), so it could not derive the federal
fallback from the canonical source.

This module has ONLY a ``tax_data`` dependency (``TaxYearData`` /
``TaxBracket``) -- no province dependency -- so it is importable at any point
in the ``countries.canada`` import sequence, including during the re-entrant
call. ``countries/canada/__init__.py`` re-exports ``federal_all_years`` from
here so existing callers (``from countries.canada import federal_all_years``)
are unchanged; the fallback builder imports it directly from this module so
it can derive from the single canonical source even mid-import.
"""

from tax_data import TaxBracket, TaxYearData


def federal_all_years():
    """Return federal TaxYearData for all known years.

    DP#20: Data is year-versioned. Each year has its own brackets,
    limits, and CPP parameters from CRA publications.
    """
    return [
        TaxYearData(
            year=2026, country="canada", province="federal",
            federal_brackets=[
                TaxBracket(0, 58523, 0.14, "14% (reduced Jul 2025)"),
                TaxBracket(58523, 117045, 0.205, "20.5%"),
                TaxBracket(117045, 181440, 0.26, "26%"),
                TaxBracket(181440, 258482, 0.29, "29% (+0.29% BPA phaseout)"),
                TaxBracket(258482, 0, 0.33, "33%"),
            ],
            rrsp_limit=33810,
            tfsa_limit=7000,
            cpp_max_pensionable=74600,       # CRA 2026: YMPE = $74,600
            cpp_rate=0.0595,
            cpp_exemption=3500,
            cpp_max_benefit_65=18092,           # Max CPP retirement pension at 65 (2026)
            basic_personal_amount=16452,
            bpa_phaseout_threshold=181440,
            bpa_phaseout_end=258482,
            bpa_minimum=14829,
            canada_employment_amount=1501,
            # DP#20, DP#27: Year-versioned DTC rates and capital gains inclusion
            federal_eligible_dtc_rate=0.150198,
            federal_non_eligible_dtc_rate=0.090301,
            federal_eligible_gross_up=0.38,
            federal_non_eligible_gross_up=0.15,
            capital_gains_inclusion_rate=0.50,
            capital_gains_upper_inclusion_rate=2/3,  # 66.67% for gains above $250K
            capital_gains_threshold=250000,
            # DP#20: CPP2 second earnings ceiling (YMPE2)
            cpp2_max_pensionable=81900,    # CRA 2026: YMPE2 = $81,900
            cpp2_rate=0.04,                  # CPP2 contribution rate 4% (2024+)
            # DP#52: QPP-specific parameters (zero for federal — only Quebec uses QPP)
            qpp_rate=0.0,
            qpp_max_benefit_65=0,
            qpp_survivor_flat_rate=0,
            source="fallback",
        ),
        TaxYearData(
            year=2025, country="canada", province="federal",
            federal_brackets=[
                TaxBracket(0, 57375, 0.145, "14.5% (blended)"),
                TaxBracket(57375, 114750, 0.205, "20.5%"),
                TaxBracket(114750, 177882, 0.26, "26%"),
                TaxBracket(177882, 253414, 0.29, "29% (+BPA phaseout)"),
                TaxBracket(253414, 0, 0.33, "33%"),
            ],
            rrsp_limit=32783,
            tfsa_limit=7000,
            cpp_max_pensionable=71300,       # CRA 2025: YMPE = $71,300
            cpp_rate=0.0595,
            cpp_exemption=3500,
            cpp_max_benefit_65=14448,           # Max CPP retirement pension at 65 (2025)
            basic_personal_amount=16129,
            bpa_phaseout_threshold=177882,
            bpa_phaseout_end=253414,
            bpa_minimum=14538,
            canada_employment_amount=1471,
            federal_eligible_dtc_rate=0.150198,
            federal_non_eligible_dtc_rate=0.090301,
            federal_eligible_gross_up=0.38,
            federal_non_eligible_gross_up=0.15,
            capital_gains_inclusion_rate=0.50,
            capital_gains_upper_inclusion_rate=2/3,
            capital_gains_threshold=250000,
            # DP#20: CPP2 second earnings ceiling (YMPE2)
            cpp2_max_pensionable=81200,    # CRA 2025: YMPE2 = $81,200
            cpp2_rate=0.04,                  # CPP2 contribution rate 4% (2024+)
            # DP#52: QPP-specific parameters (zero for federal — only Quebec uses QPP)
            qpp_rate=0.0,
            qpp_max_benefit_65=0,
            qpp_survivor_flat_rate=0,
            source="fallback",
        ),
        TaxYearData(
            year=2024, country="canada", province="federal",
            # CRA 2024 federal brackets (indexation factor 1.047 / +4.7%):
            # https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/income-tax/reducing-remuneration-subject-income-tax.html
            # 29% threshold was previously recorded as 154,906 and the 33% as
            # 220,000 — both years stale. Correct 2024 values: 173,205 / 246,752
            # (issue #748). The 29% lower bound also pegs the AMT basic exemption
            # (ITA s.127.51) and the BPA phase-out start (ITA s.118(1.1)).
            federal_brackets=[
                TaxBracket(0, 55867, 0.15, "15%"),
                TaxBracket(55867, 111733, 0.205, "20.5%"),
                TaxBracket(111733, 173205, 0.26, "26%"),
                TaxBracket(173205, 246752, 0.29, "29%"),
                TaxBracket(246752, 0, 0.33, "33%"),
            ],
            rrsp_limit=31546,
            tfsa_limit=7000,
            cpp_max_pensionable=68500,
            cpp_rate=0.0595,
            cpp_exemption=3500,
            cpp_max_benefit_65=14448,           # Max CPP retirement pension at 65 (2024)
            basic_personal_amount=15705,
            bpa_phaseout_threshold=173205,  # CRA 2024: 29% bracket lower bound (ITA s.118(1.1))
            bpa_phaseout_end=246752,
            bpa_minimum=14156,
            canada_employment_amount=1433,  # CRA 2024: $1,433 (T4127 Table 8.2)
            federal_eligible_dtc_rate=0.150198,
            federal_non_eligible_dtc_rate=0.090301,
            federal_eligible_gross_up=0.38,
            federal_non_eligible_gross_up=0.15,
            capital_gains_inclusion_rate=0.50,
            capital_gains_upper_inclusion_rate=2/3,  # 66.67% enacted June 25, 2024
            capital_gains_threshold=250000,
            # DP#20: CPP2 second earnings ceiling (YMPE2)
            cpp2_max_pensionable=73200,    # CRA 2024: YMPE2 = $73,200 (first year of second ceiling)
            cpp2_rate=0.04,                  # CPP2 contribution rate 4%
            # DP#52: QPP-specific parameters
            qpp_rate=0.0,
            qpp_max_benefit_65=0,
            qpp_survivor_flat_rate=0,
            source="fallback",
        ),
        TaxYearData(
            year=2023, country="canada", province="federal",
            # CRA 2023 federal brackets (indexation factor 1.063 / +6.3%),
            # same canada.ca source as 2024. The 26%/29%/33% thresholds were
            # previously recorded as 109,936 / 154,906 / 154,906 — stale and
            # internally inconsistent (a $3,219-wide 26% band). Correct 2023
            # values: 165,430 / 235,675 (issue #748). These already matched
            # bpa_phaseout_threshold / bpa_phaseout_end below.
            federal_brackets=[
                TaxBracket(0, 53359, 0.15, "15%"),
                TaxBracket(53359, 106717, 0.205, "20.5%"),
                TaxBracket(106717, 165430, 0.26, "26%"),
                TaxBracket(165430, 235675, 0.29, "29%"),
                TaxBracket(235675, 0, 0.33, "33%"),
            ],
            rrsp_limit=30450,
            tfsa_limit=6500,
            cpp_max_pensionable=66600,
            cpp_rate=0.0595,
            cpp_exemption=3500,
            cpp_max_benefit_65=14010,           # Max CPP retirement pension at 65 (2023)
            basic_personal_amount=15000,
            bpa_phaseout_threshold=165430,
            bpa_phaseout_end=235675,
            bpa_minimum=13520,  # CRA 2023 Federal Worksheet Line 30000
            canada_employment_amount=1368,  # CRA 2023: $1,368 (T4127 Table 8.2)
            federal_eligible_dtc_rate=0.150198,
            federal_non_eligible_dtc_rate=0.090301,
            federal_eligible_gross_up=0.38,
            federal_non_eligible_gross_up=0.15,
            capital_gains_inclusion_rate=0.50,  # Flat 50% before 2024
            capital_gains_upper_inclusion_rate=0.0,  # No tier before 2024
            capital_gains_threshold=0.0,
            # DP#20: CPP2 existed in 2023 but YMPE2 = YMPE (no second ceiling yet)
            cpp2_max_pensionable=66600,    # CRA 2023: YMPE2 = YMPE = $66,600
            cpp2_rate=0.04,                  # CPP2 contribution rate 4%
            # DP#52: QPP-specific parameters
            qpp_rate=0.0,
            qpp_max_benefit_65=0,
            qpp_survivor_flat_rate=0,
            source="fallback",
        ),
    ]