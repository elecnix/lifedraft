#!/usr/bin/env python3
"""Provinces package — auto-discovers province modules."""

# Province code → module class mapping
# Adding a new province = adding a .py file or package here + adding to this dict
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

PROVINCE_MODULES = {
    "quebec": QuebecTaxData,
    "ontario": OntarioTaxData,
    "qc": QuebecTaxData,
    "on": OntarioTaxData,
}