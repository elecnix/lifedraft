#!/usr/bin/env python3
"""Ontario tax credits, surtax, and levies — pure functions.

Models:
- Ontario surtax (levied on basic Ontario tax, not on taxable income)
- Ontario Health Premium (separate levy based on taxable income)
- Ontario Sales Tax Credit (OSTC) — the refundable component of the
  Ontario Trillium Benefit (OTB)
- Ontario Low-income Individuals and Families Tax (LIFT) credit

All functions are pure (DP#3): same inputs → same outputs.
Data is year-versioned (DP#20), loaded from tax_data via TaxDataProvider,
and structured as data not code branches (DP#8).

Per DP#10: this module owns the Ontario-specific credit/levy calculations.

References:
    Surtax: https://www.taxtips.ca/taxrates/on.htm
    Health Premium: https://www.ontario.ca/page/health-premium
    Ontario Trillium Benefit / OSTC:
        https://www.canada.ca/en/revenue-agency/services/child-family-benefits/provincial-territorial-programs/province-ontario.html
    LIFT credit: https://www.ontario.ca/page/low-income-workers-tax-credit
"""

from __future__ import annotations

from typing import Optional

from tax_data import TaxDataProvider, TaxYearData


def _get_ontario_data(year: int, provider: Optional[TaxDataProvider] = None) -> TaxYearData:
    """Load Ontario TaxYearData for a given year.

    Args:
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)
    """
    if provider is None:
        provider = TaxDataProvider()
    return provider.get_year_data(year, 'canada', 'ontario')


def ontario_surtax(
    basic_ontario_tax: float,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Ontario surtax levied on *basic Ontario tax* (after non-refundable credits).

    Two tiers stack (Ontario Tax Act, 2007, s.23):
    - rate_1 (20%) on basic ON tax above threshold_1
    - rate_2 (36%, additional) on basic ON tax above threshold_2

    Pure function (DP#3): same inputs → same output. Negative tax → 0.

    Args:
        basic_ontario_tax: Ontario tax after non-refundable credits, before surtax
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Total Ontario surtax (tier 1 + tier 2)
    """
    if basic_ontario_tax <= 0:
        return 0.0
    data = _get_ontario_data(year, provider)
    tier1 = data.on_surtax_rate_1 * max(0.0, basic_ontario_tax - data.on_surtax_threshold_1)
    tier2 = data.on_surtax_rate_2 * max(0.0, basic_ontario_tax - data.on_surtax_threshold_2)
    return tier1 + tier2


def ontario_health_premium(
    taxable_income: float,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Ontario Health Premium — a separate levy based on taxable income.

    Not a surtax: it is computed directly from taxable income (Income Tax
    Act (Ontario) s.2.2). Each tier is (lower, base, rate, cap):

        premium = min(cap, base + rate × (income − lower))

    Income at or below the lowest tier floor ($20,000) pays $0.

    Pure function (DP#3): same inputs → same output.

    Args:
        taxable_income: Taxable income for the year
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual Ontario Health Premium
    """
    if taxable_income <= 0:
        return 0.0
    data = _get_ontario_data(year, provider)
    tiers = data.on_health_premium_tiers
    if not tiers:
        return 0.0
    premium = 0.0
    for lower, base, rate, cap in tiers:
        if taxable_income > lower:
            premium = min(cap, base + rate * (taxable_income - lower))
        else:
            break
    return premium


def ontario_sales_tax_credit(
    adjusted_family_net_income: float,
    num_adults: int = 1,
    num_children: int = 0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Ontario Sales Tax Credit (OSTC) — refundable Ontario Trillium component.

    Maximum = amount_per_person × (adults + children). The credit is reduced
    by reduction_rate × (AFNI − threshold), where the threshold is the single
    threshold for a single adult with no children, otherwise the family
    threshold. Credit floors at 0.

    Pure function (DP#3): same inputs → same output.

    Args:
        adjusted_family_net_income: Adjusted family net income (AFNI)
        num_adults: Number of adults in the family (1 or 2)
        num_children: Number of dependent children
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual OSTC amount
    """
    if num_adults < 0:
        num_adults = 0
    if num_children < 0:
        num_children = 0
    income = max(0.0, adjusted_family_net_income)
    data = _get_ontario_data(year, provider)
    max_credit = data.on_ostc_amount_per_person * (num_adults + num_children)
    if max_credit <= 0:
        return 0.0
    is_single = (num_adults <= 1 and num_children == 0)
    threshold = data.on_ostc_single_threshold if is_single else data.on_ostc_family_threshold
    reduction = data.on_ostc_reduction_rate * max(0.0, income - threshold)
    return max(0.0, max_credit - reduction)


def ontario_trillium_benefit(
    adjusted_family_net_income: float,
    num_adults: int = 1,
    num_children: int = 0,
    oeptc: float = 0.0,
    noec: float = 0.0,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Ontario Trillium Benefit (OTB) total.

    The OTB combines three credits:
    - Ontario Sales Tax Credit (OSTC) — computed here from AFNI/family size
    - Ontario Energy and Property Tax Credit (OEPTC) — caller-supplied, as it
      depends on rent/property tax/energy costs not modeled here
    - Northern Ontario Energy Credit (NOEC) — caller-supplied (residence-based)

    Pure function (DP#3): same inputs → same output.

    Args:
        adjusted_family_net_income: Adjusted family net income (AFNI)
        num_adults: Number of adults in the family
        num_children: Number of dependent children
        oeptc: Pre-computed OEPTC amount (default 0)
        noec: Pre-computed NOEC amount (default 0)
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Total annual Ontario Trillium Benefit
    """
    ostc = ontario_sales_tax_credit(
        adjusted_family_net_income=adjusted_family_net_income,
        num_adults=num_adults,
        num_children=num_children,
        year=year,
        provider=provider,
    )
    return ostc + max(0.0, oeptc) + max(0.0, noec)


def ontario_lift_credit(
    employment_income: float,
    individual_net_income: float,
    family_net_income: Optional[float] = None,
    year: int = 2026,
    provider: Optional[TaxDataProvider] = None,
) -> float:
    """Ontario Low-income Individuals and Families Tax (LIFT) credit.

    Non-refundable credit (ON428-A). Maximum = min(lift_max, rate × employment
    income). The credit is reduced by reduction_rate × the *greater* of:
    - (individual net income − individual_threshold)
    - (family net income − family_threshold)
    Credit floors at 0.

    Pure function (DP#3): same inputs → same output.

    Args:
        employment_income: Employment (and self-employment) income
        individual_net_income: Adjusted individual net income
        family_net_income: Adjusted family net income. Defaults to the
            individual net income when None (single filer).
        year: Tax year
        provider: Optional TaxDataProvider (avoids repeated construction)

    Returns:
        Annual LIFT credit amount
    """
    if employment_income <= 0:
        return 0.0
    if family_net_income is None:
        family_net_income = individual_net_income
    data = _get_ontario_data(year, provider)
    base_credit = min(data.on_lift_max, data.on_lift_rate * employment_income)
    if base_credit <= 0:
        return 0.0
    indiv_excess = max(0.0, individual_net_income - data.on_lift_individual_threshold)
    family_excess = max(0.0, family_net_income - data.on_lift_family_threshold)
    reduction = data.on_lift_reduction_rate * max(indiv_excess, family_excess)
    return max(0.0, base_credit - reduction)
