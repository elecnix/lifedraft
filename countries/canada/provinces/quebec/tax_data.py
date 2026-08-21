#!/usr/bin/env python3
"""Quebec provincial tax data module.

Provides Quebec-specific tax brackets, abatement, credits, and rules
as data, not code branches.

Sources:
- Revenu Québec 2025/2026 tax brackets
- Quebec abatement: 16.5% (ITA s.8(1))
- QESI incentive rates
- Quebec solidarity tax credit: https://www.revenuquebec.ca/en/individuals/tax-credits/solidarity-tax-credit/
- Quebec QPIP: https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/quebec-parental-insurance-plan-qpip/
- Quebec FSS: https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/health-services-fund/
- Quebec non-refundable credits: Schedule B of TP-1

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Quebec Provincial Tax entry
    https://www.revenuquebec.ca/en/individuals/income-tax/income-tax-rates/
    https://justepourtous.revenuquebec.ca/en/subjects/quebec-education-savings-incentive
"""

from tax_data import TaxYearData, TaxBracket


class QuebecTaxData:
    """Quebec provincial tax data provider."""

    PROVINCE = "quebec"
    ABATEMENT = 0.165  # 16.5% Quebec abatement on federal tax
    BASIC_PERSONAL_AMOUNT_2026 = 17383
    BASIC_PERSONAL_AMOUNT_2025 = 17183

    @classmethod
    def all_years(cls) -> list:
        """Return all known year data for Quebec."""
        return [cls.year_2026(), cls.year_2025(), cls.year_2024(), cls.year_2023()]

    @classmethod
    def year_2026(cls) -> TaxYearData:
        return TaxYearData(
            year=2026, country="canada", province="quebec",
            provincial_brackets=[
                TaxBracket(0, 54345, 0.14, "14%"),
                TaxBracket(54345, 108680, 0.19, "19%"),
                TaxBracket(108680, 132245, 0.24, "24%"),
                TaxBracket(132245, 0, 0.2575, "25.75%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=cls.BASIC_PERSONAL_AMOUNT_2026,
            cpp_max_pensionable=74600,   # YMPE 2026
            cpp_rate=0.0595,
            cpp2_max_pensionable=81900,  # DP#20: CPP2 YAMPE 2026
            cpp2_rate=0.04,
            cpp_max_benefit_65=18092,
            qpp_rate=0.0640,            # DP#52: QPP rate 6.40% (higher than CPP 5.95%)
            qpp_max_benefit_65=17334,   # DP#52: QPP max benefit at 65 (2026)
            qpp_survivor_flat_rate=6498, # DP#52: QPP survivor flat-rate (annual, 2026)
            oas_annual_max=8908,
            oas_clawback_threshold=95323,
            provincial_eligible_dtc_rate=0.11510,
            provincial_non_eligible_dtc_rate=0.05575,
            # ── Quebec solidarity tax credit (2025-2026 benefit year) ──
            # Progressive reduction: 3% up to high threshold, 6% above
            # Source: Revenu Québec, Schedule E
            qc_solidarity_single_max=1028,
            qc_solidarity_couple_max=1515,
            qc_solidarity_single_threshold=40225,
            qc_solidarity_couple_threshold=50310,
            qc_solidarity_reduction_rate_low=0.03,
            qc_solidarity_reduction_rate_high=0.06,
            qc_solidarity_high_threshold=56738,
            # ── Quebec health services fund (FSS) ──
            qc_fss_self_employed_rate=0.0165,  # 1.65% for 2026
            # FSS employer rates by sector (Revenu Québec, 2026):
            # primary/manufacturing small payroll 1.25%, services small payroll 1.65%,
            # max 4.26%. Upper payroll threshold fixed at $7.8M for 2026 (not indexed).
            qc_fss_employer_rate_max=0.0426,
            qc_fss_employer_rate_primary_min=0.0125,
            qc_fss_employer_rate_services_min=0.0165,
            qc_fss_employer_payroll_lower=1_000_000,
            qc_fss_employer_payroll_upper=7_800_000,
            # Individual contribution to HSF (line 446), 2026:
            qc_fss_individual_exemption=18500,
            qc_fss_individual_first_cap=150,
            qc_fss_individual_second_threshold=64355,
            qc_fss_individual_max=1000,
            qc_fss_individual_rate=0.01,
            # ── QPIP (Quebec Parental Insurance Plan) ──
            # Source: Quebec fall economic update Nov 25, 2025 (13% reduction)
            # https://www.quebec.ca/nouvelles/actualites/details/baisse-des-taux-de-cotisation-au-regime-quebecois-dassurance-parentale-en-2026
            qpip_employee_rate=0.00430,         # 0.430% employee
            qpip_employer_rate=0.00602,        # 0.602% employer
            qpip_self_employed_rate=0.00764,   # 0.764% self-employed
            qpip_max_insurable_earnings=103000,  # 2026 max insurable
            # ── Quebec non-refundable credits ──
            qc_charitable_donation_threshold=200,
            qc_charitable_donation_rate_low=0.20,
            qc_charitable_donation_rate_high=0.24,  # 24% on above-$200 not in top bracket
            # Top donation rate 25.75% applies to donations above $200 to the
            # extent the donor has taxable income in the top (25.75%) bracket.
            qc_charitable_donation_rate_top=0.2575,
            qc_charitable_donation_top_threshold=132245,  # top bracket floor 2026
            qc_medical_expense_threshold_pct=0.03,
            # ── Senior assistance tax credit (Parameters 2026) ──
            qc_senior_assistance_max_per_person=2000,
            qc_senior_assistance_threshold_single=28405,
            qc_senior_assistance_threshold_couple=46200,
            qc_senior_assistance_reduction_rate=0.0547,  # 5.47% (2026)
            # ── Amount with respect to age / living alone / retirement (2026) ──
            qc_age_amount=3986,
            qc_living_alone_amount=2172,
            qc_retirement_income_amount=3541,
            qc_age_credit_reduction_threshold=42955,
            qc_age_credit_reduction_rate=0.1875,  # 18.75%
            qc_non_refundable_credit_rate=0.14,   # lowest QC rate (14%)
            qc_tuition_credit_rate=0.08,   # issue #783: TP-1 Schedule T line 45 (8% specific rate, not 14%)
            # ── Work premium (general, persons without children, 2026) ──
            qc_work_premium_max_single=1207.33,
            qc_work_premium_max_couple=1882.45,
            qc_work_premium_excluded_single=2400,
            qc_work_premium_excluded_couple=3600,
            qc_work_premium_growth_rate=0.116,    # 11.6%
            qc_work_premium_reduction_threshold_single=12808,
            qc_work_premium_reduction_threshold_couple=19828,
            qc_work_premium_reduction_rate=0.10,  # 10%
            # ── Prescription drug insurance (RAMQ 2025-2026 benefit year) ──
            qc_drug_insurance_max_premium=766,
            # ── RESP/CESG/QESI/CLB thresholds (DP#12, DP#20) ──
            cesg_first_threshold=58523,
            cesg_second_threshold=117045,
            qesi_first_threshold=54345,
            qesi_second_threshold=108680,
            clb_threshold_1_3_children=58523,
            clb_threshold_4_children=66078,
            clb_threshold_5plus_children=73633,
            source="fallback",
        )

    @classmethod
    def year_2025(cls) -> TaxYearData:
        return TaxYearData(
            year=2025, country="canada", province="quebec",
            provincial_brackets=[
                # Source: https://www.revenuquebec.ca/en/individuals/income-tax/income-tax-rates/
                # 2025 brackets: $0–$53,255 / $53,255–$106,495 / $106,495–$129,590 / $129,590+
                TaxBracket(0, 53255, 0.14, "14%"),
                TaxBracket(53255, 106495, 0.19, "19%"),
                TaxBracket(106495, 129590, 0.24, "24%"),
                TaxBracket(129590, 0, 0.2575, "25.75%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=cls.BASIC_PERSONAL_AMOUNT_2025,
            cpp_max_pensionable=68500,
            cpp_rate=0.0595,
            cpp2_max_pensionable=81200,  # DP#20: CPP2 YAMPE 2025
            cpp2_rate=0.04,
            cpp_max_benefit_65=14448,
            qpp_rate=0.0640,            # DP#52: QPP rate 6.40% (2025)
            qpp_max_benefit_65=17334,   # DP#52: QPP max benefit at 65 (2025)
            qpp_survivor_flat_rate=6498, # DP#52: QPP survivor flat-rate (annual, 2025)
            oas_annual_max=8381,
            oas_clawback_threshold=90997,
            provincial_eligible_dtc_rate=0.11510,
            provincial_non_eligible_dtc_rate=0.05575,
            # ── Quebec solidarity tax credit (2024-2025 benefit year) ──
            qc_solidarity_single_max=929,
            qc_solidarity_couple_max=1380,
            qc_solidarity_single_threshold=38079,
            qc_solidarity_couple_threshold=47598,
            qc_solidarity_reduction_rate_low=0.03,
            qc_solidarity_reduction_rate_high=0.06,
            qc_solidarity_high_threshold=50050,
            # ── Quebec health services fund (FSS) ──
            qc_fss_self_employed_rate=0.0165,  # 1.65% for 2025
            qc_fss_employer_rate_max=0.0426,
            qc_fss_employer_rate_primary_min=0.0125,
            qc_fss_employer_rate_services_min=0.0165,
            qc_fss_employer_payroll_lower=1_000_000,
            qc_fss_employer_payroll_upper=7_800_000,
            qc_fss_individual_exemption=18130,
            qc_fss_individual_first_cap=150,
            qc_fss_individual_second_threshold=63060,
            qc_fss_individual_max=1000,
            qc_fss_individual_rate=0.01,
            # ── QPIP ──
            # Source: RQAP official rate page
            qpip_employee_rate=0.00494,         # 0.494% employee
            qpip_employer_rate=0.00692,        # 0.692% employer (1.4× employee)
            qpip_self_employed_rate=0.00878,   # 0.878% self-employed (without sickness)
            qpip_max_insurable_earnings=98000,  # 2025 max insurable
            # ── Quebec non-refundable credits ──
            qc_charitable_donation_threshold=200,
            qc_charitable_donation_rate_low=0.20,
            qc_charitable_donation_rate_high=0.24,  # 24% on above-$200 not in top bracket
            qc_charitable_donation_rate_top=0.2575,
            qc_charitable_donation_top_threshold=129590,  # top bracket floor 2025
            qc_medical_expense_threshold_pct=0.03,
            qc_tuition_credit_rate=0.08,   # issue #783: TP-1 Schedule T line 45 (8% specific rate, not 14%)
            # ── Senior assistance tax credit (Parameters 2025) ──
            qc_senior_assistance_max_per_person=2000,
            qc_senior_assistance_threshold_single=27835,
            qc_senior_assistance_threshold_couple=45270,
            qc_senior_assistance_reduction_rate=0.0540,  # 5.40% (2025)
            # ── Amount with respect to age / living alone / retirement (2025) ──
            qc_age_amount=3906,
            qc_living_alone_amount=2128,
            qc_retirement_income_amount=3470,
            qc_age_credit_reduction_threshold=42090,
            qc_age_credit_reduction_rate=0.1875,
            qc_non_refundable_credit_rate=0.14,
            # ── Work premium (general, persons without children, 2025) ──
            qc_work_premium_max_single=1185.52,
            qc_work_premium_max_couple=1848.34,
            qc_work_premium_excluded_single=2400,
            qc_work_premium_excluded_couple=3600,
            qc_work_premium_growth_rate=0.116,
            qc_work_premium_reduction_threshold_single=12620,
            qc_work_premium_reduction_threshold_couple=19534,
            qc_work_premium_reduction_rate=0.10,
            # ── Prescription drug insurance (RAMQ 2024-2025 benefit year) ──
            qc_drug_insurance_max_premium=755,
            # ── RESP/CESG/QESI/CLB thresholds (DP#12, DP#20) ──
            cesg_first_threshold=57375,
            cesg_second_threshold=114750,
            qesi_first_threshold=53255,
            qesi_second_threshold=106495,
            clb_threshold_1_3_children=57375,
            clb_threshold_4_children=64733,
            clb_threshold_5plus_children=72123,
            source="fallback",
        )

    @classmethod
    def year_2024(cls) -> TaxYearData:
        return TaxYearData(
            year=2024, country="canada", province="quebec",
            provincial_brackets=[
                # Source: https://www.revenuquebec.ca/en/individuals/income-tax/income-tax-rates/
                # 2024 brackets: $0–$51,780 / $51,780–$103,545 / $103,545–$126,000 / $126,000+
                TaxBracket(0, 51780, 0.14, "14%"),
                TaxBracket(51780, 103545, 0.19, "19%"),
                TaxBracket(103545, 126000, 0.24, "24%"),
                TaxBracket(126000, 0, 0.2575, "25.75%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=17183,
            cpp_max_pensionable=68500,
            cpp_rate=0.0595,
            cpp2_max_pensionable=73200,  # DP#20: CPP2 YAMPE 2024 (first year of second ceiling)
            cpp2_rate=0.04,
            cpp_max_benefit_65=14448,
            qpp_rate=0.0640,            # DP#52: QPP rate 6.40% (2024)
            qpp_max_benefit_65=17334,   # DP#52: QPP max benefit at 65 (2024)
            qpp_survivor_flat_rate=6498, # DP#52: QPP survivor flat-rate (annual, 2024)
            oas_annual_max=8291,
            oas_clawback_threshold=87068,
            provincial_eligible_dtc_rate=0.11510,
            provincial_non_eligible_dtc_rate=0.05575,
            # ── Quebec solidarity tax credit (2023-2024 benefit year) ──
            qc_solidarity_single_max=800,
            qc_solidarity_couple_max=1204,
            qc_solidarity_single_threshold=36450,
            qc_solidarity_couple_threshold=44970,
            qc_solidarity_reduction_rate_low=0.03,
            qc_solidarity_reduction_rate_high=0.06,
            qc_solidarity_high_threshold=46595,
            # ── Quebec health services fund (FSS) ──
            qc_fss_self_employed_rate=0.0165,  # 1.65% for 2024
            # ── QPIP ──
            # Source: RQAP official rate page
            qpip_employee_rate=0.00494,         # 0.494% employee
            qpip_employer_rate=0.00692,        # 0.692% employer (1.4× employee)
            qpip_self_employed_rate=0.00878,   # 0.878% self-employed (without sickness)
            qpip_max_insurable_earnings=94000,  # 2024 max insurable
            # ── Quebec non-refundable credits ──
            qc_charitable_donation_threshold=200,
            qc_charitable_donation_rate_low=0.20,
            qc_charitable_donation_rate_high=0.24,  # 24% on above-$200 not in top bracket
            qc_medical_expense_threshold_pct=0.03,
            qc_tuition_credit_rate=0.08,   # issue #783: TP-1 Schedule T line 45 (8% specific rate, not 14%)
            # ── RESP/CESG/QESI/CLB thresholds (DP#12, DP#20) ──
            cesg_first_threshold=55867,
            cesg_second_threshold=111733,
            qesi_first_threshold=51780,
            qesi_second_threshold=103545,
            clb_threshold_1_3_children=55867,
            clb_threshold_4_children=63036,
            clb_threshold_5plus_children=70234,
            source="fallback",
        )

    @classmethod
    def year_2023(cls) -> TaxYearData:
        return TaxYearData(
            year=2023, country="canada", province="quebec",
            provincial_brackets=[
                # Source: https://www.revenuquebec.ca/en/individuals/income-tax/income-tax-rates/
                # 2023 brackets: $0–$49,275 / $49,275–$98,540 / $98,540–$119,910 / $119,910+
                TaxBracket(0, 49275, 0.14, "14%"),
                TaxBracket(49275, 98540, 0.19, "19%"),
                TaxBracket(98540, 119910, 0.24, "24%"),
                TaxBracket(119910, 0, 0.2575, "25.75%"),
            ],
            provincial_abatement=cls.ABATEMENT,
            basic_personal_amount=15980,
            cpp_max_pensionable=66600,
            cpp_rate=0.0595,
            cpp2_max_pensionable=66600,  # DP#20: CPP2 YAMPE 2023 (= YMPE, no second ceiling yet)
            cpp2_rate=0.04,
            cpp_max_benefit_65=14010,
            qpp_rate=0.0640,            # DP#52: QPP rate 6.40% (2023)
            qpp_max_benefit_65=15170,   # DP#52: QPP max benefit at 65 (2023)
            qpp_survivor_flat_rate=5640, # DP#52: QPP survivor flat-rate (annual, 2023)
            oas_annual_max=8083,
            oas_clawback_threshold=83917,
            provincial_eligible_dtc_rate=0.11510,
            provincial_non_eligible_dtc_rate=0.05575,
            # ── Quebec solidarity tax credit (2022-2023 benefit year) ──
            qc_solidarity_single_max=743,
            qc_solidarity_couple_max=1126,
            qc_solidarity_single_threshold=34190,
            qc_solidarity_couple_threshold=41930,
            qc_solidarity_reduction_rate_low=0.03,
            qc_solidarity_reduction_rate_high=0.06,
            qc_solidarity_high_threshold=42367,
            # ── Quebec health services fund (FSS) ──
            qc_fss_self_employed_rate=0.0165,  # 1.65% for 2023
            # ── QPIP ──
            # Source: RQAP official rate page; Martel Desjardins tax alert 2023
            qpip_employee_rate=0.00494,         # 0.494% employee
            qpip_employer_rate=0.00692,        # 0.692% employer (1.4× employee)
            qpip_self_employed_rate=0.00878,   # 0.878% self-employed (without sickness)
            qpip_max_insurable_earnings=91000,  # 2023 max insurable
            # ── Quebec non-refundable credits ──
            qc_charitable_donation_threshold=200,
            qc_charitable_donation_rate_low=0.20,
            qc_charitable_donation_rate_high=0.24,  # 24% on above-$200 not in top bracket
            qc_medical_expense_threshold_pct=0.03,
            qc_tuition_credit_rate=0.08,   # issue #783: TP-1 Schedule T line 45 (8% specific rate, not 14%)
            source="fallback",
        )
