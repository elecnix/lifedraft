#!/usr/bin/env python3
"""Quebec tax credits and contributions — pure functions.

Models:
- Quebec solidarity tax credit (crédit de solidarité)
- Quebec health services fund contribution (FSS)
- Quebec Parental Insurance Plan (QPIP) premiums
- Quebec non-refundable credits (charitable donations, medical expenses)

All functions are pure (DP#3): same inputs → same outputs.
Data is year-versioned (DP#20), loaded from tax_data via TaxDataProvider.

Per DP#10: this module owns the Quebec-specific credit calculations.

References:
    https://www.revenuquebec.ca/en/individuals/tax-credits/solidarity-tax-credit/
    https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/health-services-fund/
    https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/quebec-parental-insurance-plan-qpip/
    Quebec TP-1 Schedule B (non-refundable credits)
"""

from __future__ import annotations

from typing import Dict, Optional

from tax_data import TaxDataProvider, TaxYearData


def _get_quebec_data(year: int, provider: Optional[TaxDataProvider] = None) -> TaxYearData:
    """Load Quebec TaxYearData for a given year.

    Args:
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)
    """
    if provider is None:
        provider = TaxDataProvider()
    return provider.get_year_data(year, 'canada', 'quebec')


def quebec_solidarity_credit(
    income: float,
    is_couple: bool = False,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec solidarity tax credit (crédit de solidarité).

    Refundable credit with progressive reduction rates:
    - 3% on net family income between threshold and high threshold
    - 6% on net family income above high threshold

    Pure function (DP#3): same inputs → same output.

    Args:
        income: Net family income for the year
        is_couple: True if married/civil union (uses couple thresholds)
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual solidarity credit amount
    """
    if income < 0:
        income = 0
    data = _get_quebec_data(year, provider)
    if is_couple:
        max_credit = data.qc_solidarity_couple_max
        threshold = data.qc_solidarity_couple_threshold
    else:
        max_credit = data.qc_solidarity_single_max
        threshold = data.qc_solidarity_single_threshold

    rate_low = data.qc_solidarity_reduction_rate_low
    rate_high = data.qc_solidarity_reduction_rate_high
    high_threshold = data.qc_solidarity_high_threshold

    if income <= threshold:
        return max_credit
    if income <= high_threshold:
        reduction = rate_low * (income - threshold)
        return max(0, max_credit - reduction)
    # Income above high threshold: 3% on first bracket, 6% on excess
    reduction_low = rate_low * (high_threshold - threshold)
    reduction_high = rate_high * (income - high_threshold)
    return max(0, max_credit - reduction_low - reduction_high)


def quebec_health_services_fund(
    self_employment_income: float = 0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec health services fund (FSS) contribution for self-employed.

    Self-employed individuals pay FSS on their QPP-eligible income,
    capped at the YMPE. Employees do not pay FSS directly (employers do).
    Negative self-employment income is treated as 0.

    Pure function (DP#3): same inputs → same output.

    Args:
        self_employment_income: Net self-employment income
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual FSS contribution
    """
    if self_employment_income <= 0:
        return 0.0
    data = _get_quebec_data(year, provider)
    max_pensionable = data.cpp_max_pensionable
    rate = data.qc_fss_self_employed_rate
    capped_income = min(self_employment_income, max_pensionable)
    return capped_income * rate


def quebec_qpip_premium(
    insurable_earnings: float,
    is_self_employed: bool = False,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec Parental Insurance Plan (QPIP) premium.

    Employee pays employee share; self-employed pay both shares.
    Negative insurable earnings are treated as 0.

    Pure function (DP#3): same inputs → same output.

    Args:
        insurable_earnings: Annual insurable earnings
        is_self_employed: True for self-employed (pays full rate)
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual QPIP premium
    """
    if insurable_earnings <= 0:
        return 0.0
    data = _get_quebec_data(year, provider)
    max_insurable = data.qpip_max_insurable_earnings
    if is_self_employed:
        rate = data.qpip_self_employed_rate
    else:
        rate = data.qpip_employee_rate
    capped_earnings = min(insurable_earnings, max_insurable)
    return capped_earnings * rate


def quebec_charitable_donation_credit(
    donations: float,
    taxable_income: Optional[float] = None,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec non-refundable credit for charitable donations.

    Verified Quebec structure (issue #323, DP#12):
    - 20% on the first $200 of donations
    - On donations above $200: 25.75% to the extent the donor has taxable
      income taxed at the top (25.75%) bracket, and 24% on the remainder.

    Per Revenu Québec, the 25.75% rate applies only to the portion of
    above-$200 donations that does not exceed the donor's taxable income in
    the top bracket; the rest is credited at 24%. When ``taxable_income`` is
    None, no income is in the top bracket, so all above-$200 donations are
    credited at 24% (the standard high rate). The prior flat 40% high rate
    was incorrect and is replaced.

    Negative donations are treated as 0. Pure function (DP#3).

    Args:
        donations: Total charitable donations in the year
        taxable_income: Donor's taxable income (enables the 25.75% top rate)
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Non-refundable tax credit amount

    Source: https://www.revenuquebec.ca/en/citizens/tax-credits/tax-credit-for-donations-and-gifts/
    """
    if donations <= 0:
        return 0.0
    data = _get_quebec_data(year, provider)
    low_rate = data.qc_charitable_donation_rate_low      # 20%
    high_rate = data.qc_charitable_donation_rate_high     # 24%
    top_rate = data.qc_charitable_donation_rate_top       # 25.75%
    top_threshold = data.qc_charitable_donation_top_threshold
    threshold = data.qc_charitable_donation_threshold     # $200

    if donations <= threshold:
        return donations * low_rate

    credit = threshold * low_rate
    above = donations - threshold

    # Portion of above-$200 donations eligible for the 25.75% top rate equals
    # the donor's taxable income in the top bracket, capped at `above`.
    top_eligible = 0.0
    if taxable_income is not None and taxable_income > top_threshold:
        top_eligible = min(above, taxable_income - top_threshold)
    credit += top_eligible * top_rate
    credit += (above - top_eligible) * high_rate
    return credit


def quebec_senior_assistance_credit(
    family_income: float,
    eligible_persons: int = 1,
    is_couple: bool = False,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec senior assistance tax credit (crédit d'impôt pour le soutien aux aînés).

    Refundable credit for individuals 70 or over (and their eligible spouse).
    Maximum is per eligible person; the combined maximum is reduced by a
    fixed rate on family income above the applicable threshold (single vs
    couple). Per Revenu Québec, a person is eligible only if 70+; this
    function takes the eligible-person count and couple status as inputs.

    Pure function (DP#3): same inputs → same output.

    Args:
        family_income: Net family income for the year
        eligible_persons: Number of persons 70+ claiming (0, 1, or 2)
        is_couple: True if a couple (uses the couple reduction threshold)
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Annual senior assistance credit amount

    Source: https://www.revenuquebec.ca/en/citizens/tax-credits/senior-assistance-tax-credit/
    """
    if eligible_persons <= 0:
        return 0.0
    data = _get_quebec_data(year, provider)
    max_credit = data.qc_senior_assistance_max_per_person * min(eligible_persons, 2)
    threshold = (data.qc_senior_assistance_threshold_couple if is_couple
                 else data.qc_senior_assistance_threshold_single)
    income = max(0.0, family_income)
    if income <= threshold:
        return max_credit
    reduction = data.qc_senior_assistance_reduction_rate * (income - threshold)
    return max(0.0, max_credit - reduction)


def quebec_work_premium(
    work_income: float,
    family_income: float,
    is_couple: bool = False,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec general work premium (prime au travail) for persons without children.

    Refundable credit for low-income workers. The premium grows at a fixed
    rate on work income above an excluded amount, up to a maximum, then is
    reduced at a fixed rate on family income above a reduction threshold.

    This models the GENERAL work premium for households WITHOUT children
    (the standard case). The adapted premium (severely limited capacity)
    and household-with-children variants use different parameters and are
    out of scope here (documented assumption).

    Pure function (DP#3): same inputs → same output.

    Args:
        work_income: Eligible work income (employment + business)
        family_income: Net family income for the reduction test
        is_couple: True for a couple without children; False for a person alone
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Annual work premium amount

    Source: https://www.revenuquebec.ca/en/citizens/tax-credits/work-premium-tax-credits/
    """
    data = _get_quebec_data(year, provider)
    if is_couple:
        max_credit = data.qc_work_premium_max_couple
        excluded = data.qc_work_premium_excluded_couple
        reduction_threshold = data.qc_work_premium_reduction_threshold_couple
    else:
        max_credit = data.qc_work_premium_max_single
        excluded = data.qc_work_premium_excluded_single
        reduction_threshold = data.qc_work_premium_reduction_threshold_single

    work = max(0.0, work_income)
    if work <= excluded:
        return 0.0
    # Growth phase: rate × (work income − excluded), capped at the maximum
    premium = min(max_credit, data.qc_work_premium_growth_rate * (work - excluded))
    # Reduction phase: reduce on family income above the threshold
    income = max(0.0, family_income)
    if income > reduction_threshold:
        premium -= data.qc_work_premium_reduction_rate * (income - reduction_threshold)
    return max(0.0, premium)


def quebec_drug_insurance_premium(
    is_covered_by_private_plan: bool = False,
    income_tested_fraction: float = 1.0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec public prescription drug insurance plan premium (line 447).

    A contribution (not a credit) payable by adults covered by the RAMQ
    public plan. The actual premium is income-tested between $0 and the
    annual maximum per adult. This models the maximum premium scaled by an
    income-tested fraction (1.0 = maximum; 0.0 = exempt). People covered by
    a private plan for the full year pay no premium.

    Pure function (DP#3): same inputs → same output.

    Args:
        is_covered_by_private_plan: True if covered by a private plan (premium 0)
        income_tested_fraction: Fraction of the max premium owed (0.0–1.0)
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Annual drug insurance premium owed

    Source: https://www.ramq.gouv.qc.ca/en/citizens/prescription-drug-insurance/rates-effect
    """
    if is_covered_by_private_plan:
        return 0.0
    data = _get_quebec_data(year, provider)
    fraction = min(1.0, max(0.0, income_tested_fraction))
    return data.qc_drug_insurance_max_premium * fraction


def quebec_health_services_fund_individual(
    income: float,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec individual contribution to the Health Services Fund (line 446).

    Payable by Quebec residents whose income (excluding most employment income
    and government benefits) exceeds an exemption. Two-bracket formula:
    - first bracket: lesser of $150 or 1% of income above the exemption
    - second bracket: lesser of the overall max ($1,000) or $150 + 1% of
      income above the second-bracket threshold

    Pure function (DP#3): same inputs → same output.

    Args:
        income: Income subject to the contribution (excludes employment income)
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Annual individual HSF contribution

    Source: https://www.revenuquebec.ca/en/citizens/income-tax-return/paying-a-balance-due-or-receiving-a-refund/paying-contributions-and-premiums/individual-contributions-to-the-health-services-fund/
    """
    data = _get_quebec_data(year, provider)
    income = max(0.0, income)
    exemption = data.qc_fss_individual_exemption
    if income <= exemption:
        return 0.0
    rate = data.qc_fss_individual_rate
    first_cap = data.qc_fss_individual_first_cap
    second_threshold = data.qc_fss_individual_second_threshold
    if income <= second_threshold:
        return min(first_cap, rate * (income - exemption))
    return min(data.qc_fss_individual_max,
               first_cap + rate * (income - second_threshold))


def quebec_health_services_fund_employer(
    total_payroll: float,
    sector: str = "services",
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec employer contribution to the Health Services Fund (FSS), by sector.

    The rate varies by total payroll and sector. Below the lower payroll
    threshold employers pay the sector minimum rate (primary/manufacturing
    lower than services); at/above the upper threshold all employers pay the
    maximum rate; between the thresholds the rate increases linearly. This is
    the standard, year-versioned data-driven model (DP#8, DP#20).

    Pure function (DP#3): same inputs → same output.

    Args:
        total_payroll: Total Quebec payroll for the year
        sector: 'primary', 'manufacturing' (reduced min) or 'services'/'other'
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Annual employer FSS contribution

    Source: https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/calculating-source-deductions-and-contributions/employer-contribution-to-the-health-services-fund/total-payroll-threshold-and-health-services-fund-contribution-rate/
    """
    payroll = max(0.0, total_payroll)
    rate = quebec_fss_employer_rate(payroll, sector, year, provider)
    return payroll * rate


def quebec_fss_employer_rate(
    total_payroll: float,
    sector: str = "services",
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec FSS employer contribution RATE for a given payroll and sector.

    Returns the applicable rate (not the dollar amount). Primary and
    manufacturing sectors get a reduced minimum rate; all other ("services")
    sectors use the standard minimum. The rate rises linearly with payroll
    between the lower and upper thresholds, reaching the maximum at/above the
    upper threshold.

    Pure function (DP#3): same inputs → same output.
    """
    data = _get_quebec_data(year, provider)
    payroll = max(0.0, total_payroll)
    if sector in ("primary", "manufacturing"):
        min_rate = data.qc_fss_employer_rate_primary_min
    else:
        min_rate = data.qc_fss_employer_rate_services_min
    max_rate = data.qc_fss_employer_rate_max
    lower = data.qc_fss_employer_payroll_lower
    upper = data.qc_fss_employer_payroll_upper

    if payroll <= lower:
        return min_rate
    if payroll >= upper:
        return max_rate
    # Linear interpolation between the lower and upper payroll thresholds
    span = upper - lower
    if span <= 0:
        return max_rate
    return min_rate + (max_rate - min_rate) * (payroll - lower) / span


def quebec_age_amount_credit(
    family_income: float,
    age: int,
    lives_alone: bool = False,
    retirement_income: float = 0.0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec non-refundable credit: age amount + living alone + retirement income.

    These three amounts (line 361) share a single family-income reduction
    threshold and reduction rate. The age amount applies at 65+, the
    living-alone amount to a person living alone, and the retirement-income
    amount up to the eligible retirement income. The summed amounts are
    reduced on family income above the threshold, then converted to a credit
    at the lowest QC rate (14%).

    Pure function (DP#3): same inputs → same output.

    Args:
        family_income: Net family income for the reduction test
        age: Owner's age (age amount applies at 65+)
        lives_alone: True if a person living alone
        retirement_income: Eligible retirement income (caps the retirement amount)
        year: Tax year
        provider: Optional TaxDataProvider

    Returns:
        Non-refundable credit amount (already converted at the QC rate)

    Source: https://www.revenuquebec.ca/en/citizens/tax-credits/age-amount-amount-for-a-person-living-alone-and-amount-for-retirement-income/
    """
    data = _get_quebec_data(year, provider)
    amount = 0.0
    if age >= 65:
        amount += data.qc_age_amount
    if lives_alone:
        amount += data.qc_living_alone_amount
    if retirement_income > 0:
        amount += min(data.qc_retirement_income_amount, retirement_income)
    if amount <= 0:
        return 0.0
    income = max(0.0, family_income)
    if income > data.qc_age_credit_reduction_threshold:
        amount -= data.qc_age_credit_reduction_rate * (income - data.qc_age_credit_reduction_threshold)
        amount = max(0.0, amount)
    return amount * data.qc_non_refundable_credit_rate


def quebec_medical_expense_credit(
    medical_expenses: float,
    net_income: float,
    lowest_mtr: float,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Quebec non-refundable credit for medical expenses.

    Eligible amount = medical expenses - threshold_pct × net income.
    Credit = eligible amount × lowest marginal rate in the year.
    Negative medical expenses are treated as 0.

    Pure function (DP#3): same inputs → same output.

    Args:
        medical_expenses: Total eligible medical expenses
        net_income: Net income for threshold calculation
        lowest_mtr: Lowest marginal rate (non-refundable credit rate)
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Non-refundable tax credit amount
    """
    if medical_expenses <= 0:
        return 0.0
    data = _get_quebec_data(year, provider)
    threshold_pct = data.qc_medical_expense_threshold_pct
    threshold = max(0, net_income) * threshold_pct
    eligible = max(0, medical_expenses - threshold)
    return eligible * lowest_mtr


def quebec_non_refundable_credits(
    net_income: float,
    charitable_donations: float = 0,
    medical_expenses: float = 0,
    lowest_mtr: float = 0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> Dict[str, float]:
    """Aggregate Quebec non-refundable tax credits.

    Pure function (DP#3): same inputs → same output.

    Args:
        net_income: Net income for threshold calculations
        charitable_donations: Total charitable donations
        medical_expenses: Total eligible medical expenses
        lowest_mtr: Lowest marginal rate. The default of 0 is a sentinel
            requiring explicit override — callers must pass the actual
            lowest QC MTR for their tax year (DP#13).
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Dict with individual credit amounts and total
    """
    donation_credit = quebec_charitable_donation_credit(
        donations=charitable_donations, year=year, provider=provider)
    medical_credit = quebec_medical_expense_credit(
        medical_expenses=medical_expenses,
        net_income=net_income,
        lowest_mtr=lowest_mtr,
        year=year,
        provider=provider)
    return {
        'charitable_donation_credit': donation_credit,
        'medical_expense_credit': medical_credit,
        'total_credit': donation_credit + medical_credit,
    }
