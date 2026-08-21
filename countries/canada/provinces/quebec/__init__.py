from countries.canada.provinces.quebec.tax_data import QuebecTaxData
from countries.canada.provinces.quebec.quebec_deduction import (
    QuebecDeductionTracker,
    compute_sm_qc_benefit,
    quebec_interest_deduction,
    quebec_sm_portfolio_optimization,
)
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_solidarity_credit,
    quebec_health_services_fund,
    quebec_qpip_premium,
    quebec_non_refundable_credits,
    quebec_charitable_donation_credit,
    quebec_medical_expense_credit,
    quebec_senior_assistance_credit,
    quebec_work_premium,
    quebec_drug_insurance_premium,
    quebec_health_services_fund_individual,
    quebec_health_services_fund_employer,
    quebec_fss_employer_rate,
    quebec_age_amount_credit,
)
from countries.canada.provinces.quebec.quebec_lif import (
    quebec_lif_maximum_withdrawal,
    quebec_lif_withdrawal_range,
    quebec_lif_temporary_income_max,
    quebec_lif_annuity_conversion,
)

__all__ = [
    "QuebecTaxData",
    "QuebecDeductionTracker",
    "compute_sm_qc_benefit",
    "quebec_interest_deduction",
    "quebec_sm_portfolio_optimization",
    "quebec_solidarity_credit",
    "quebec_health_services_fund",
    "quebec_qpip_premium",
    "quebec_non_refundable_credits",
    "quebec_charitable_donation_credit",
    "quebec_medical_expense_credit",
    "quebec_senior_assistance_credit",
    "quebec_work_premium",
    "quebec_drug_insurance_premium",
    "quebec_health_services_fund_individual",
    "quebec_health_services_fund_employer",
    "quebec_fss_employer_rate",
    "quebec_age_amount_credit",
    "quebec_lif_maximum_withdrawal",
    "quebec_lif_withdrawal_range",
    "quebec_lif_temporary_income_max",
    "quebec_lif_annuity_conversion",
]
