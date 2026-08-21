#!/usr/bin/env python3
"""Canadian-specific tax calculation convenience functions.

These functions encode Canadian (and Quebec-specific) tax rules such as
federal/provincial bracket computations, RRSP strategies, dividend
gross-up/DTC, withholding-tax drag, and asset-location heuristics.

For generic, jurisdiction-agnostic tax functions (marginal_rate,
tax_on_income, effective_tax_rate, capital_gains_rate, etc.) see
``tax_calculator`` in the project root.

Usage:
    from countries.canada.tax_calc import (
        federal_tax, quebec_tax, rrsp_deduction_savings, QC_ABATEMENT,
    )

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Federal/Provincial Tax Brackets
    Federal tax brackets (ITA Part I):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals-tax-years.html
    Eligible dividends (ITA s.82 gross-up 38%, s.121 federal DTC 15.0198%):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/eligible-dividends.html
    Quebec abatement (ITA s.8(1), 16.5%):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-return/line-40500-foreign-tax-credit.html
    Canada Employment Amount (ITA s.8(1.1), line 31260):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/line-31260-canada-employment-amount.html
    Basic Personal Amount (ITA s.118(1), line 30000):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/line-30000-basic-personal-amount.html
    Medical expense tax credit (ITA s.118.2, line 33099/33199):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-33099.html
    Charitable donation tax credit (ITA s.118.1, line 34000/35000):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-34000.html
    CPP/QPP basic exemption (ITA s.114/Schedule 8, CPP Act s.20):
        https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/canada-pension-plan-cpp.html
"""

from tax_data import default_tax_provider
from dataclasses import dataclass
from typing import Optional

from tax_calculator import (
    TaxDataProvider,
    )

# =============================================================================
# Canadian-specific constants
# =============================================================================

QC_ABATEMENT = 0.165  # 16.5% Quebec abatement on federal tax

# CRA medical-expense credit threshold cap (ITA s.118.2, line 33099) — the
# lesser-of ``min(3% × net_income, cap)`` ceiling. Indexed annually by CRA; no
# year-versioned slot exists in ``TaxYearData`` (issue #973), so the published
# anchors live here as the single spelling (DP#9). Projected years use the
# same integer-round projection the inline dict used before relocation, so
# the computed cap is byte-identical to the prior inline form.
# Source: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-33099.html
_CRA_MEDICAL_THRESHOLD_CAP_BY_YEAR: dict[int, int] = {
    2023: 2635,  # CRA line 33099 lesser-of ceiling
    2024: 2759,  # CRA line 33099 lesser-of ceiling
    2025: 2834,  # CRA line 33099 lesser-of ceiling
    2026: 2902,  # CRA line 33099 lesser-of ceiling
}

# US-Canada treaty withholding-tax rate on foreign (non-Canadian) dividends
# held outside a treaty-exempt account (issue #975). 15% is the reduced
# treaty rate a non-resident qualifies for via IRS Form W-8BEN; the
# international-ETF branch of ``withholding_tax_drag`` leaks it unrecoverable
# in TFSA/RESP. No year-versioned slot exists in ``TaxYearData``; the
# US-listed branch already takes this rate as its ``wht_rate_us`` parameter,
# so the international branch mirrors it with ``wht_rate_international``
# defaulting to this constant (DP#9: one spelling of the treaty rate).
# Source: Canada-United States Income Tax Convention, Article XV
# (reduced withholding on dividends, 15% for the general portfolio rate).
WHT_TREATY_RATE = 0.15


# =============================================================================
# Internal: bracket-based tax computation
# =============================================================================

def _tax_on_brackets(income: float, brackets) -> float:
    """Compute tax from a list of TaxBracket objects.

    Pure function (DP#3): same inputs → same outputs.
    """
    tax = 0.0
    for b in brackets:
        upper = b.max_income if b.max_income else float('inf')
        if income <= b.min_income:
            break
        taxable = min(income, upper) - b.min_income
        tax += max(0, taxable) * b.rate
        if income <= upper:
            break
    return tax


def _load_fed_data(year: int, provider: TaxDataProvider = None):
    """Load federal TaxYearData, returning (fed_data, lowest_rate, bpa, cea)."""
    if provider is None:
        provider = TaxDataProvider()
    fed_data = provider._load_year(year, 'canada', 'federal')
    lowest_rate = fed_data.federal_brackets[0].rate if fed_data.federal_brackets else 0.15
    bpa = fed_data.basic_personal_amount
    cea = fed_data.canada_employment_amount
    return fed_data, lowest_rate, bpa, cea


# =============================================================================
# Federal tax — with and without Quebec abatement
# =============================================================================

def federal_tax_before_abatement(income: float, year: int = 2026,
                                 _province: str = "quebec",
                                 provider: TaxDataProvider = None) -> float:
    """Federal tax before any provincial abatement.

    The _province parameter is unused — federal brackets don't vary by
    province. It exists for API symmetry with federal_tax() so callers
    can pass the same arguments to both functions.

    DP#3: Pure function — same inputs always produce same output.
    """
    fed_data, _, _, _ = _load_fed_data(year, provider)
    return _tax_on_brackets(income, fed_data.federal_brackets)


def quebec_abatement_amount(income: float, year: int = 2026,
                             province: str = "quebec",
                             provider: TaxDataProvider = None) -> float:
    """Compute the Quebec abatement amount (16.5% of pre-abatement federal tax).

    For non-Quebec provinces, the abatement is 0.

    DP#3: Pure function.
    """
    if province.lower() not in ('quebec', 'qc'):
        return 0.0
    if provider is None:
        provider = TaxDataProvider()
    before = federal_tax_before_abatement(income, year, province, provider)
    abatement_rate = QC_ABATEMENT
    try:
        prov_data = provider._load_year(year, 'canada', province)
        abatement_rate = prov_data.provincial_abatement
    except ValueError:
        pass
    return before * abatement_rate


def federal_tax(income: float, year: int = 2026,
                province: str = "quebec",
                provider: TaxDataProvider = None) -> float:
    """Federal tax for a resident of the given province.

    Applies the provincial abatement (16.5% for Quebec) to reduce
    federal tax. The abatement is now computed as a separate step:
    federal_tax = federal_tax_before_abatement - quebec_abatement.

    DP#3: Pure function — same inputs always produce same output.
    """
    before = federal_tax_before_abatement(income, year, province, provider)
    abatement = quebec_abatement_amount(income, year, province, provider)
    return before - abatement


def combined_tax_separate(income: float, year: int = 2026,
                          province: str = "quebec",
                          provider: TaxDataProvider = None) -> float:
    """Combined federal + provincial tax with abatement as separate step.

    Computes: federal_before_abatement - abatement + provincial_tax

    DP#3: Pure function.
    """
    fed_before = federal_tax_before_abatement(income, year, province, provider)
    abatement = quebec_abatement_amount(income, year, province, provider)
    if province.lower() in ('quebec', 'qc'):
        prov_tax = quebec_tax(income, year, provider)
    else:
        prov_tax = _provincial_tax(income, year, province, provider)
    return fed_before - abatement + prov_tax


def _provincial_tax(income: float, year: int, province: str,
                    provider: TaxDataProvider = None) -> float:
    """Provincial tax for any province (not just Quebec)."""
    if provider is None:
        provider = TaxDataProvider()
    prov_data = provider._load_year(year, 'canada', province)
    return _tax_on_brackets(income, prov_data.provincial_brackets)


# =============================================================================
# Quebec provincial tax
# =============================================================================

def quebec_tax(income: float, year: int = 2026,
               provider: TaxDataProvider = None) -> float:
    """Quebec provincial tax."""
    if provider is None:
        provider = TaxDataProvider()
    prov_data = provider._load_year(year, 'canada', 'quebec')
    return _tax_on_brackets(income, prov_data.provincial_brackets)


# =============================================================================
# Non-refundable tax credits (federal only)
# =============================================================================

def tuition_tax_credit(tuition: float,
                       year: int = 2026,
                       provider: TaxDataProvider = None,
                       province: Optional[str] = None) -> float:
    """Tuition tax credit -- the SIMPLE case of issue #764, extended in #783
    to include the Quebec PROVINCIAL tuition credit: a student claims their
    OWN non-refundable credit(s) on the eligible tuition they paid.

    Federal portion (ITA s.118.5, line 32000): ``tuition × federal
    lowest_rate`` (15% in 2026). The rate is the federal lowest bracket rate,
    sourced from TaxDataProvider (DP#12/DP#20) via ``_load_fed_data`` -- it
    is NOT a hardcoded constant.

    Quebec provincial portion (TP-1 Schedule T, line 45, issue #783): when
    ``province`` is ``'quebec'``/``'qc'``, ``tuition × qc_tuition_credit_rate``
    (a SPECIFIC 8% rate, NOT the 14% general lowest-bracket rate). The rate
    is year-versioned data on the Quebec ``TaxYearData``
    (``qc_tuition_credit_rate``), sourced from Revenu Québec -- NOT hardcoded.
    A non-Quebec resident (``province`` is None or another province) gets the
    FEDERAL credit only; the provincial portion is 0 (DP#32: never credit a
    provincial portion at a guessed rate). Both credits are non-refundable
    (the caller floors the combined reduction at 0).

    DP#3: Pure function. DP#8/DP#20: parameterized by jurisdiction; the rate
    is year-versioned data, sourced not hardcoded.

    Args:
        tuition: Eligible tuition paid in the year (>= 0).
        year: Tax year.
        provider: Optional TaxDataProvider override.
        province: The student's province of residence. ``'quebec'``/``'qc'``
            adds the QC provincial tuition credit; any other value (or None,
            the default) yields the federal credit only -- preserving the
            pre-#783 behaviour for every existing caller.

    Returns:
        The combined federal + (Quebec provincial, if applicable) tax savings
        from the tuition credit (0 for 0 tuition).
    """
    if tuition is None or tuition <= 0:
        return 0.0
    if provider is None:
        provider = TaxDataProvider()
    try:
        _, lowest_rate, _, _ = _load_fed_data(year, provider)
    except (ValueError, IndexError):
        lowest_rate = 0.15  # DP#13: round fallback (matches _load_fed_data's own)
    credit = tuition * lowest_rate
    # Issue #783: Quebec PROVINCIAL tuition credit (TP-1 Schedule T, 8%). Only
    # for a Quebec resident -- a non-QC student gets the federal credit only.
    if province is not None and province.lower() in ('quebec', 'qc'):
        try:
            qc_data = provider._load_year(year, 'canada', 'quebec')
            qc_rate = qc_data.qc_tuition_credit_rate
        except (ValueError, IndexError, AttributeError):
            qc_rate = 0.0  # DP#32: no data -> no provincial credit, not a guess
        # DP#32: a missing/zero QC rate yields $0 provincial credit (loud
        # absence, not a guessed 8%); the rate must be in the data to fire.
        credit += tuition * qc_rate
    return credit


def canada_employment_credit(employment_income: float,
                             year: int = 2026,
                             provider: TaxDataProvider = None) -> float:
    """Canada Employment Amount non-refundable credit (line 31260).

    Tax savings = min(employment_income, credit_amount) × lowest_rate.
    Both credit_amount and lowest_rate come from TaxDataProvider (DP#12).

    DP#3: Pure function.
    DP#20: Credit amount and rate are year-versioned data.

    Args:
        employment_income: Employment income (T4 income)
        year: Tax year
        provider: Optional TaxDataProvider override

    Returns:
        Tax savings from the Canada Employment Amount credit
    """
    if provider is None:
        provider = TaxDataProvider()
    try:
        _, lowest_rate, _, cea = _load_fed_data(year, provider)
        credit_amount = cea
    except (ValueError, IndexError):
        credit_amount = 1500  # DP#13: round fallback
        lowest_rate = 0.15  # DP#13: round fallback
    capped = min(employment_income, credit_amount)
    if capped <= 0:
        return 0.0
    return capped * lowest_rate


def basic_personal_amount_credit(basic_personal_amount: float,
                                 lowest_rate: float,
                                 income: float | None = None,
                                 bpa_phaseout_threshold: float = 0,
                                 bpa_phaseout_end: float = 0,
                                 bpa_minimum: float = 0) -> float:
    """Basic Personal Amount non-refundable credit.

    Tax savings = effective_bpa × lowest_rate.

    For income below the BPA, claimable = income (unused portion
    transferable to spouse). For income above the phaseout threshold,
    the enhanced BPA portion reduces linearly to bpa_minimum at
    bpa_phaseout_end.

    CRA formula (ITA s.118(1.1)):
        BPA = min_bpa + enhanced × (1 - (income - threshold) / (end - threshold))
    where enhanced = max_bpa - min_bpa.

    DP#1: Income (not age) determines BPA eligibility.
    DP#3: Pure function.
    DP#20: Phaseout threshold, end, and minimum are year-versioned.

    Args:
        basic_personal_amount: Maximum BPA for the tax year
        lowest_rate: Lowest bracket rate
        income: Taxable income (limits claim at low income). None = skip
            both low-income cap and phaseout, using full BPA.
        bpa_phaseout_threshold: Income above which BPA enhancement phases out
        bpa_phaseout_end: Income at which BPA reaches minimum
        bpa_minimum: Floor BPA after full phaseout

    Returns:
        Tax savings from the BPA credit
    """
    effective_bpa = basic_personal_amount
    if income is not None:
        if bpa_phaseout_threshold > 0 and bpa_phaseout_end > 0 and income > bpa_phaseout_threshold:
            if income >= bpa_phaseout_end:
                effective_bpa = bpa_minimum
            else:
                enhanced = basic_personal_amount - bpa_minimum
                fraction = (income - bpa_phaseout_threshold) / (bpa_phaseout_end - bpa_phaseout_threshold)
                reduction = enhanced * fraction
                effective_bpa = basic_personal_amount - reduction

        # Low-income cap: can only claim up to actual income
        claimable = min(effective_bpa, max(0, income))
    else:
        claimable = basic_personal_amount
    return claimable * lowest_rate


def optimize_bpa_transfer(primary_income: float,
                          spouse_income: float,
                          basic_personal_amount: float,
                          lowest_rate: float,
                          primary_federal_tax: float | None = None,
                          bpa_phaseout_threshold: float = 0,
                          bpa_phaseout_end: float = 0,
                          bpa_minimum: float = 0) -> dict[str, float]:
    """Optimize Basic Personal Amount transfer between spouses.

    When one spouse has income below the BPA, the unused portion
    can be transferred to the other spouse (ITA s.118(2)).

    Non-refundable credits cannot reduce tax below zero. The
    spouse_transferable amount is capped by the primary earner's
    remaining federal tax room (primary's federal tax after their
    own BPA credit). If primary_federal_tax is not provided (None), falls
    back to primary_income × lowest_rate as an approximation.

    The returned `total_credits` represents the total credit amount
    available, which may exceed actual tax owed. Actual tax savings
    are bounded by total tax liability.

    DP#3: Pure function — same inputs always produce same output.
    DP#1: Income (not age) determines BPA eligibility.

    Args:
        primary_income: Primary earner's taxable income
        spouse_income: Spouse's taxable income
        basic_personal_amount: Federal BPA for the tax year
        lowest_rate: Lowest bracket rate (from TaxDataProvider)
        primary_federal_tax: Primary's actual federal tax after abatement.
            Used to cap transfer. If None (default), approximated from
            primary_income × lowest_rate.

    Returns:
        Dict with primary_credit, spouse_credit, spouse_transferable,
        total_credits
    """
    primary_credit = basic_personal_amount_credit(
        basic_personal_amount, lowest_rate, primary_income,
        bpa_phaseout_threshold=bpa_phaseout_threshold,
        bpa_phaseout_end=bpa_phaseout_end,
        bpa_minimum=bpa_minimum,
    )
    spouse_direct_credit = basic_personal_amount_credit(
        basic_personal_amount, lowest_rate, spouse_income,
        bpa_phaseout_threshold=bpa_phaseout_threshold,
        bpa_phaseout_end=bpa_phaseout_end,
        bpa_minimum=bpa_minimum,
    )

    spouse_unused = max(0, basic_personal_amount - max(0, spouse_income))
    raw_spouse_transferable = spouse_unused * lowest_rate

    # Cap by primary's remaining federal tax room (non-refundable limit)
    if primary_federal_tax is not None:
        primary_tax_room = max(0, primary_federal_tax - primary_credit)
    else:
        primary_tax_room = max(0, primary_income * lowest_rate - primary_credit)
    spouse_transferable = min(raw_spouse_transferable, primary_tax_room)

    primary_total = primary_credit + spouse_transferable

    total_credits = primary_total + spouse_direct_credit

    return {
        'primary_credit': primary_total,
        'spouse_credit': spouse_direct_credit,
        'spouse_transferable': spouse_transferable,
        'total_credits': total_credits,
    }


def compute_non_refundable_credits(employment_income: float,
                                   taxable_income: float | None = None,
                                   year: int = 2026,
                                   province: str = "quebec",
                                   provider: TaxDataProvider = None,
                                   medical_expenses: float = 0,
                                   charitable_donations: float = 0,
                                   net_income: float | None = None) -> dict[str, float]:
    """Aggregate all federal non-refundable tax credits.

    Includes:
    - Basic Personal Amount (BPA) — reduced when taxable_income < BPA
    - Canada Employment Amount
    - Medical expense credit (ITA s.118.2) — only when medical_expenses > 0
    - Charitable donation credit (ITA s.118.1) — only when donations > 0

    Federal credits reduce federal tax only, not provincial. Medical and
    charitable inputs default to 0, so existing callers see UNCHANGED totals
    (additive, issue #315).

    DP#3: Pure function.
    DP#12: Data from TaxDataProvider, not hardcoded dicts.
    DP#20: Credit amounts are year-versioned.

    Args:
        employment_income: Employment income
        taxable_income: Taxable income (reduces BPA credit if below BPA).
            None = use full BPA without phaseout or low-income cap.
        year: Tax year
        province: Province code
        provider: Optional TaxDataProvider override
        medical_expenses: Eligible medical expenses (default 0).
        charitable_donations: Eligible charitable donations (default 0).
        net_income: Net income for the medical 3% threshold. Defaults to
            taxable_income when None.

    Returns:
        Dict with individual credit amounts and total
    """
    if provider is None:
        provider = TaxDataProvider()

    try:
        fed_data, lowest_rate, bpa, _ = _load_fed_data(year, provider)
        bpa_threshold = fed_data.bpa_phaseout_threshold
        bpa_end = fed_data.bpa_phaseout_end
        bpa_min = fed_data.bpa_minimum
    except (ValueError, IndexError):
        lowest_rate = 0.15  # DP#13: round fallback
        bpa = 16000  # DP#13: round fallback
        bpa_threshold = 0
        bpa_end = 0
        bpa_min = 0

    ce_credit = canada_employment_credit(employment_income, year, provider)
    bpa_credit = basic_personal_amount_credit(
        bpa, lowest_rate, taxable_income,
        bpa_phaseout_threshold=bpa_threshold,
        bpa_phaseout_end=bpa_end,
        bpa_minimum=bpa_min,
    )

    med_net_income = net_income if net_income is not None else taxable_income
    medical_credit = medical_expense_credit(
        medical_expenses, med_net_income or 0, year, provider=provider,
    ) if medical_expenses else 0.0
    donation_credit = charitable_donation_credit(
        charitable_donations, taxable_income, year, provider=provider,
    ) if charitable_donations else 0.0

    total = bpa_credit + ce_credit + medical_credit + donation_credit
    return {
        'basic_personal_amount': bpa_credit,
        'canada_employment': ce_credit,
        'medical_expense': medical_credit,
        'charitable_donation': donation_credit,
        'total': total,
    }


# =============================================================================
# Medical expense, charitable donation, and CPP exemption (issue #315)
# Year-versioned federal parameters (DP#8, DP#20) kept local to this module,
# following the AMTParameters pattern — they are federal credit constants, not
# bracket data, and do not vary by province.
# =============================================================================

@dataclass(frozen=True)
class FederalCreditParameters:
    """Year-versioned federal non-refundable credit parameters.

    DP#8: compose through data. DP#20: year-versioned. DP#13: round-number
    fallbacks where a precise indexed figure is not pinned to a CRA table.

    Attributes:
        year: Tax year.
        lowest_rate: Lowest federal bracket rate (credit conversion rate, 15%
            pre-2025 reduction, 14% for 2026). Most s.118.x credits convert at
            this rate.
        medical_threshold_pct: Medical expense threshold as % of net income
            (ITA s.118.2: lesser of this % of net income or the fixed cap).
        medical_threshold_cap: Fixed-dollar cap on the medical threshold
            (CRA line 33099 lesser-of test).
        charitable_first_tier_limit: Donation amount taxed at the low rate
            ($200, ITA s.118.1).
        charitable_low_rate: Credit rate on the first $200 of donations (= lowest
            federal rate).
        charitable_high_rate: Credit rate on donations above $200 (29% general,
            ITA s.118.1).
        charitable_top_rate: Credit rate on above-$200 donations to the extent
            of income taxed in the top (33%) bracket (s.118.1(3), 2016+).
    """
    year: int
    lowest_rate: float
    medical_threshold_pct: float
    medical_threshold_cap: float
    charitable_first_tier_limit: float
    charitable_low_rate: float
    charitable_high_rate: float
    charitable_top_rate: float

    @classmethod
    def for_year(cls, year: int, provider: TaxDataProvider = None) -> "FederalCreditParameters":
        """Build federal credit parameters for a tax year.

        The lowest-rate conversion factor is pulled from TaxDataProvider so it
        tracks the bracket data (DP#12); the structural constants (3% medical
        threshold, $200 donation split, 29%/33% donation rates) are stable CRA
        figures. The medical threshold cap is indexed: $2,759 (2024), $2,834
        (2025) per CRA; projected at 2% for other years (DP#13).

        Source: ITA s.118.1, s.118.2; CRA lines 33099 and 34000.
        """
        try:
            _, lowest_rate, _, _ = _load_fed_data(year, provider)
        except (ValueError, IndexError):
            lowest_rate = 0.15  # DP#13 round fallback

        # Medical threshold cap (CRA, indexed). The published anchors and the
        # 2% integer-round projection live in _CRA_MEDICAL_THRESHOLD_CAP_BY_YEAR
        # (DP#9 single spelling; issue #973 — no TaxYearData slot exists).
        caps = _CRA_MEDICAL_THRESHOLD_CAP_BY_YEAR
        if year in caps:
            cap = caps[year]
        else:
            nearest = min(caps, key=lambda y: abs(y - year))
            cap = round(caps[nearest] * (1.02 ** (year - nearest)))

        return cls(
            year=year,
            lowest_rate=lowest_rate,
            medical_threshold_pct=0.03,        # ITA s.118.2: 3% of net income
            medical_threshold_cap=cap,
            charitable_first_tier_limit=200.0,  # ITA s.118.1: first $200
            charitable_low_rate=lowest_rate,    # first $200 at lowest rate
            charitable_high_rate=0.29,          # above $200 general rate
            charitable_top_rate=0.33,           # s.118.1(3) top-bracket portion
        )


def medical_expense_credit(medical_expenses: float,
                           net_income: float,
                           year: int = 2026,
                           params: FederalCreditParameters = None,
                           provider: TaxDataProvider = None) -> float:
    """Federal medical expense tax credit (ITA s.118.2, line 33099).

    Eligible amount = medical_expenses - min(3% × net_income, indexed cap).
    Credit = eligible amount × lowest federal rate. Negative eligible amounts
    yield no credit.

    DP#3: pure function. DP#20: threshold/cap/rate are year-versioned.

    Args:
        medical_expenses: Total eligible medical expenses paid.
        net_income: Net income, for the 3%-of-income threshold test.
        year: Tax year.
        params: Optional pre-built FederalCreditParameters.
        provider: Optional TaxDataProvider override.

    Returns:
        Federal non-refundable tax credit (tax reduction) in dollars.

    Source: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-33099.html
    """
    if medical_expenses <= 0:
        return 0.0
    if params is None:
        params = FederalCreditParameters.for_year(year, provider)
    threshold = min(max(0.0, net_income) * params.medical_threshold_pct,
                    params.medical_threshold_cap)
    eligible = max(0.0, medical_expenses - threshold)
    return eligible * params.lowest_rate


def charitable_donation_credit(donations: float,
                               taxable_income: float = None,
                               year: int = 2026,
                               params: FederalCreditParameters = None,
                               provider: TaxDataProvider = None) -> float:
    """Federal charitable donation tax credit (ITA s.118.1, line 34000).

    Two-tier structure (DP#10, standard case):
    - First $200 of donations: credited at the lowest federal rate.
    - Donations above $200: 29% general rate, except the portion of above-$200
      donations matched by taxable income in the top (33%) bracket is credited
      at 33% (s.118.1(3), in force since 2016). When ``taxable_income`` is None,
      no income is treated as in the top bracket and all above-$200 donations
      are credited at 29%.

    DP#3: pure function. Negative donations → 0.

    Args:
        donations: Total eligible donations claimed.
        taxable_income: Taxable income (enables the 33% top-bracket portion).
        year: Tax year.
        params: Optional pre-built FederalCreditParameters.
        provider: Optional TaxDataProvider override.

    Returns:
        Federal non-refundable tax credit in dollars.

    Source: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-34000.html
    """
    if donations <= 0:
        return 0.0
    if params is None:
        params = FederalCreditParameters.for_year(year, provider)

    first_tier = params.charitable_first_tier_limit
    if donations <= first_tier:
        return donations * params.charitable_low_rate

    credit = first_tier * params.charitable_low_rate
    above = donations - first_tier

    # Top-bracket threshold (start of the 33% bracket) from bracket data.
    top_threshold = 0.0
    try:
        fed_data, _, _, _ = _load_fed_data(year, provider)
        if fed_data.federal_brackets:
            top_threshold = fed_data.federal_brackets[-1].min_income
    except (ValueError, IndexError):
        top_threshold = 0.0

    top_eligible = 0.0
    if taxable_income is not None and top_threshold > 0 and taxable_income > top_threshold:
        top_eligible = min(above, taxable_income - top_threshold)

    credit += top_eligible * params.charitable_top_rate
    credit += (above - top_eligible) * params.charitable_high_rate
    return credit


def cpp_basic_exemption_pensionable(employment_income: float,
                                    year: int = 2026,
                                    province: str = "quebec",
                                    provider: TaxDataProvider = None) -> dict[str, float]:
    """CPP/QPP pensionable earnings after the basic exemption (line 30800).

    Clarifies where the CPP/QPP basic exemption lives (issue #315): it is a
    contribution-base reduction, applied here before any contribution rate.
    Pensionable earnings = min(income, YMPE) - basic_exemption, floored at 0.
    CPP2 (the band between YMPE and YMPE2) has NO basic exemption.

    DP#3: pure function. DP#12: YMPE, exemption, and rate come from
    TaxDataProvider (Quebec data carries the QPP rate; federal carries CPP).

    Args:
        employment_income: Gross pensionable employment income.
        year: Tax year.
        province: 'quebec'/'qc' uses QPP rate; otherwise CPP rate.
        provider: Optional TaxDataProvider override.

    Returns:
        Dict with basic_exemption, pensionable_earnings, contribution_rate,
        and base_contribution (employee share on the first band).

    Source: CPP Act s.20 (Year's Basic Exemption); CRA line 30800.
    """
    if provider is None:
        provider = TaxDataProvider()
    is_quebec = province.lower() in ('quebec', 'qc')
    data = provider._load_year(year, 'canada', 'quebec' if is_quebec else 'federal')

    exemption = data.cpp_exemption
    ympe = data.cpp_max_pensionable
    rate = data.qpp_rate if (is_quebec and data.qpp_rate) else data.cpp_rate

    pensionable = max(0.0, min(employment_income, ympe) - exemption)
    return {
        'basic_exemption': exemption,
        'pensionable_earnings': pensionable,
        'contribution_rate': rate,
        'base_contribution': pensionable * rate,
    }


# =============================================================================
# Integrated tax computation with AMT and credits
# =============================================================================

def compute_total_tax(taxable_income: float,
                      employment_income: float = 0,
                      taxable_capital_gains: float = 0,
                      capital_gains_inclusion: float = 0.50,
                      carrying_charges: float = 0,
                      employment_deductions: float = 0,
                      stock_option_deduction: float = 0,
                      non_capital_loss_deducted: float = 0,
                      year: int = 2026,
                      province: str = "quebec",
                      provider: TaxDataProvider = None,
                      is_couple: bool = False,
                      self_employment_income: float = 0) -> dict[str, float]:
    """Compute total tax including non-refundable credits, Quebec refundable
    credits, and AMT.

    Federal non-refundable credits reduce federal tax only; provincial
    tax is unaffected. Combined after credits = federal after credits
    + provincial tax. AMT is compared against federal tax after credits.

    For Quebec residents, refundable credits (solidarity) reduce total tax,
    while payroll contributions (QPIP, FSS) add to total tax.

    Steps:
    1. Compute gross federal tax (before abatement) and provincial tax
    2. Apply abatement to get federal tax after abatement
    3. Subtract federal non-refundable credits from federal tax only
    4. Combined after credits = federal_after_credits + provincial_tax
    5. Compute Quebec refundable credits and payroll contributions
    6. Net Quebec effect = solidarity credit - (QPIP + FSS)
    7. Compare federal tax with AMT liability
    8. Return total tax (combined + AMT surcharge + net Quebec credits)

    DP#3: Pure function — same inputs always produce same output.
    DP#27: AMT affects deduct-later accuracy for high-income scenarios.

    Args:
        taxable_income: Taxable income after all regular deductions
        employment_income: Employment income (for Canada Employment Amount
            and QPIP)
        taxable_capital_gains: Taxable (regular-inclusion) capital gains already
            in taxable_income. 100% included for AMT (ITA s.127.52(1)(d)) — the
            dominant AMT trigger. NOTE there is deliberately no `rrsp_deduction`
            argument: an RRSP deduction is NOT an AMT preference item (#710).
        capital_gains_inclusion: Regular inclusion rate behind that figure.
        carrying_charges: s.20(1)(c)-(f)/(bb) interest — half added back (j)(ii).
        employment_deductions: s.8(1) deductions — half added back (j)(i).
        stock_option_deduction: s.110(1)(d) deduction — added back in full (h).
        non_capital_loss_deducted: s.111(1) carryovers — half added back (i).
        year: Tax year
        province: Province code
        provider: Optional TaxDataProvider override
        is_couple: True if married/civil union (affects solidarity credit)
        self_employment_income: Net self-employment income (for FSS)

    Returns:
        Dict with regular_tax, amt_surcharge, total_tax,
        non_refundable_credits, quebec_refundable_credits, breakdown
    """
    if provider is None:
        provider = TaxDataProvider()

    # Step 1: Gross federal tax and provincial tax
    gross_fed = federal_tax_before_abatement(taxable_income, year, province, provider)
    abatement = quebec_abatement_amount(taxable_income, year, province, provider)
    if province.lower() in ('quebec', 'qc'):
        prov_tax = quebec_tax(taxable_income, year, provider)
    else:
        prov_tax = _provincial_tax(taxable_income, year, province, provider)

    federal_after_abatement = gross_fed - abatement

    # Step 2: Federal non-refundable credits
    nr_credits = compute_non_refundable_credits(
        employment_income, taxable_income, year, province, provider,
    )

    # Step 3: Credits reduce federal tax only, not provincial
    federal_after_credits = max(0, federal_after_abatement - nr_credits['total'])

    # Step 4: Quebec refundable credits and contributions
    from countries.canada.provinces.quebec.quebec_credits import (
        quebec_solidarity_credit,
        quebec_qpip_premium,
        quebec_health_services_fund,
    )

    qc_solidarity = 0.0
    qc_qpip = 0.0
    qc_fss = 0.0
    if province.lower() in ('quebec', 'qc'):
        qc_solidarity = quebec_solidarity_credit(
            taxable_income, is_couple=is_couple, year=year, provider=provider)
        qc_qpip = quebec_qpip_premium(
            employment_income, is_self_employed=False, year=year, provider=provider)
        if self_employment_income > 0:
            qc_qpip += quebec_qpip_premium(
                self_employment_income, is_self_employed=True, year=year, provider=provider)
        qc_fss = quebec_health_services_fund(
            self_employment_income, year=year, provider=provider)

    quebec_refundable_credits = {
        'solidarity_credit': qc_solidarity,
        'qpip_premium': qc_qpip,
        'fss_contribution': qc_fss,
        'total': qc_solidarity - qc_qpip - qc_fss,
    }

    # Step 5: Combined = federal after credits + provincial - net Quebec credits
    net_quebec = quebec_refundable_credits['total']
    combined_after_credits = federal_after_credits + prov_tax - net_quebec

    # Step 6: AMT comparison (against federal-only tax)
    from countries.canada.amt import AMTParameters, total_tax_with_amt

    amt_result = total_tax_with_amt(
        regular_tax=federal_after_credits,
        taxable_income=taxable_income,
        taxable_capital_gains=taxable_capital_gains,
        capital_gains_inclusion=capital_gains_inclusion,
        carrying_charges=carrying_charges,
        employment_deductions=employment_deductions,
        stock_option_deduction=stock_option_deduction,
        non_capital_loss_deducted=non_capital_loss_deducted,
        params=AMTParameters.for_year(year, provider),
    )

    total_tax = combined_after_credits + amt_result['amt_surcharge']

    return {
        'regular_tax': combined_after_credits,
        'amt_surcharge': amt_result['amt_surcharge'],
        'total_tax': total_tax,
        'non_refundable_credits': nr_credits,
        'quebec_refundable_credits': quebec_refundable_credits,
        'breakdown': {
            'federal_before_abatement': gross_fed,
            'quebec_abatement': abatement,
            'provincial_tax': prov_tax,
            'federal_after_abatement': federal_after_abatement,
            'credits_total': nr_credits['total'],
            'federal_after_credits': federal_after_credits,
            'combined_before_quebec_credits': federal_after_credits + prov_tax,
            'quebec_solidarity': qc_solidarity,
            'quebec_qpip': qc_qpip,
            'quebec_fss': qc_fss,
            'net_quebec_refundable_credits': net_quebec,
            'combined_after_credits': combined_after_credits,
            'amt_tentative': amt_result['amt_details']['amt_tentative'],
            'amt_carryforward': amt_result['amt_carryforward'],
        },
    }


# =============================================================================
# RRSP deduction analysis
# =============================================================================

def rrsp_deduction_savings(deduction: float, income: float,
                           brackets: list[dict] = None,
                           year: int = 2026, province: str = "quebec") -> float:
    """Tax savings from an RRSP deduction, applied top-down through brackets."""
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)

    remaining_deduction = deduction
    tax_savings = 0.0
    remaining_income = income

    # Top-down: apply deduction starting from the highest bracket
    for b in reversed(brackets):
        if remaining_deduction <= 0:
            break
        upper = b['max'] if b['max'] else float('inf')
        if remaining_income <= b['min']:
            continue

        bracket_width = upper - b['min']
        taxed_at_this_rate = min(remaining_income - b['min'], bracket_width)

        deduct_from_bracket = min(remaining_deduction, taxed_at_this_rate)
        tax_savings += deduct_from_bracket * b['rate']
        remaining_deduction -= deduct_from_bracket
        remaining_income -= deduct_from_bracket

    return tax_savings


def rrsp_deduct_later_savings(rrsp_room: float, annual_income: float,
                               years: int = 10,
                               bracket_target: float = 0.0,
                               brackets: list[dict] = None,
                               year: int = 2026, province: str = "quebec") -> tuple[float, float]:
    """Compare deduct-now vs deduct-later RRSP strategy.

    ``bracket_target`` follows ``SimulationConfig.deduct_later_bracket_target``'s
    0-means-auto-detect convention (issue #974): 0 = not specified, not the
    real 2026 federal 26%/20.5% boundary ($117,045) it used to spell inline.
    When a target IS wanted, the caller derives it from the bracket data —
    see the auto-detect block in ``cashout_optimizer.compute_min_extraction``
    and ``bracket_ceiling`` in ``simulation_rules`` (DP#9: the boundary is
    read from the brackets, never re-spelled). This function does not itself
    consume ``bracket_target``; it computes the two savings streams purely
    from ``brackets`` via ``rrsp_deduction_savings``, so the default value
    does not affect the result.
    """
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)

    deduct_now_savings = rrsp_deduction_savings(rrsp_room, annual_income, brackets)

    annual_deduction = rrsp_room / years
    deduct_later_savings = 0.0

    remaining_room = rrsp_room
    for _yr in range(years):
        if remaining_room <= 0:
            break
        deduction = min(annual_deduction, remaining_room)
        savings = rrsp_deduction_savings(deduction, annual_income, brackets)
        deduct_later_savings += savings
        remaining_room -= deduction

    return deduct_now_savings, deduct_later_savings


def spousal_rrsp_benefit(contribution: float, contributor_rate: float,
                         withdraw_rate: float,
                         attribution_years: int = 3) -> dict:
    """Calculate the benefit of a spousal RRSP contribution."""
    deduction_savings = contribution * contributor_rate
    withdrawal_tax = contribution * withdraw_rate
    net_benefit = deduction_savings - withdrawal_tax
    bracket_spread_pct = (contributor_rate - withdraw_rate) * 100

    return {
        'contribution': contribution,
        'deduction_savings': deduction_savings,
        'withdrawal_tax': withdrawal_tax,
        'net_benefit': net_benefit,
        'bracket_spread_pct': bracket_spread_pct,
        'attribution_years': attribution_years,
        'attribution_risk': 'Clear after 3 calendar years with no contributions',
        'net_benefit_per_1000': net_benefit / contribution * 1000 if contribution > 0 else 0,
    }


def tax_on_eligible_dividend(amount: float, marginal_rate: float,
                                province: str = "quebec",
                                year: int = 2026) -> float:
    """Tax on Canadian eligible dividends (gross-up + DTC).

    Correct formula (DP#27):
        tax = grossed_up × MTR - grossed_up × fed_dtc_rate - grossed_up × prov_dtc_rate

    DP#20: DTC rates and gross-up are year-versioned data from tax_data.
    """
    from countries.canada.income_type import _get_federal_dtc_rates, _get_provincial_dtc_rates

    fed = _get_federal_dtc_rates(year)
    prov = _get_provincial_dtc_rates(province, year)

    gross_up_rate = fed['eligible_gross_up']
    fed_dtc_rate = fed['eligible_dtc_rate']
    prov_dtc_rate = prov['eligible_dtc_rate']

    grossed_up = amount * (1 + gross_up_rate)
    tax_on_grossed_up = grossed_up * marginal_rate
    dtc_federal = grossed_up * fed_dtc_rate
    dtc_provincial = grossed_up * prov_dtc_rate
    return max(0, tax_on_grossed_up - dtc_federal - dtc_provincial)


def effective_dividend_rate(marginal_rate: float,
                              province: str = "quebec",
                              year: int = 2026) -> float:
    """Compute effective tax rate on eligible dividends.

    Correct formula (DP#27):
        effective = gross_up_factor × (MTR - federal_dtc_rate - provincial_dtc_rate)
    """
    from countries.canada.income_type import _get_federal_dtc_rates, _get_provincial_dtc_rates

    fed = _get_federal_dtc_rates(year)
    prov = _get_provincial_dtc_rates(province, year)

    gross_up_factor = 1 + fed['eligible_gross_up']
    fed_dtc_rate = fed['eligible_dtc_rate']
    prov_dtc_rate = prov['eligible_dtc_rate']

    rate = gross_up_factor * (marginal_rate - fed_dtc_rate - prov_dtc_rate)
    return max(0, rate)


def withholding_tax_drag(
    account_type: str,
    etf_type: str,
    yield_pct: float = 0.02,  # DP#2/DP#13: configurable per holding, not hardcoded opinion
    wht_rate_us: float = 0.15,
    wht_rate_international: float = WHT_TREATY_RATE,
) -> float:
    """Compute the annual WHT tax drag in basis points."""
    # US-listed ETFs: treaty exempts WHT in RRSP, FTC recovers in non-reg
    if etf_type == 'us_listed':
        if account_type == 'rrsp':
            return 0.0  # Treaty exempts WHT
        elif account_type == 'tfsa':
            return wht_rate_us * yield_pct * 10000
        elif account_type == 'non_reg':
            return 0.0  # FTC recovers WHT
        elif account_type == 'resp':
            return wht_rate_us * yield_pct * 10000
    # Canadian ETFs: no WHT on Canadian distributions
    elif etf_type in ('canadian', 'canadian_dividend'):
        return 0.0
    # International ETFs: typical 15% WHT unrecoverable in TFSA/RESP
    elif etf_type == 'international':
        if account_type == 'rrsp':
            return 0.0
        elif account_type in ('tfsa', 'resp'):
            return wht_rate_international * yield_pct * 10000
        elif account_type == 'non_reg':
            return 0.0

    return 0.0


def asset_location_tax_impact(
    etf_type: str,
    marginal_rate: float,
    province: str = "quebec",
    yield_pct: float = 0.02,
) -> dict[str, float]:
    """Return tax drag (basis points/year) for an ETF type in each account.

    Per DP#30, this function provides *tax impact data* — the annual tax drag
    for each income type in each account type — rather than prescriptive advice
    about where to hold investments. The optimizer or user decides placement.

    Args:
        etf_type: One of 'us_listed', 'canadian_dividend', 'canadian',
                  'bonds', 'international'.
        marginal_rate: Combined marginal tax rate (e.g., 0.50 for 50%).
        province: Province code (affects DTC for dividends).
        yield_pct: Expected yield rate (default 0.02 = 2%).

    Returns:
        Dict mapping account type string → tax drag in basis points/year.
        E.g., {'rrsp': 0.0, 'tfsa': 30.0, 'non_reg': 0.0, 'resp': 30.0}
    """
    account_types = ['rrsp', 'tfsa', 'non_reg', 'resp']
    result = {}

    for acct in account_types:
        # WHT drag (from withholding_tax_drag)
        wht_drag = withholding_tax_drag(acct, etf_type, yield_pct)

        # Distribution tax drag (non-reg only)
        dist_drag = 0.0
        if acct == 'non_reg':
            if etf_type == 'bonds':
                # Bond interest fully taxable at MTR
                dist_drag = yield_pct * marginal_rate * 10000
            elif etf_type == 'canadian_dividend':
                # Eligible dividends: effective rate < MTR due to DTC
                eff_rate = effective_dividend_rate(marginal_rate, province)
                dist_drag = yield_pct * eff_rate * 10000
            elif etf_type in ('us_listed', 'international'):
                # Foreign dividends: fully taxable at MTR
                # WHT is recovered by FTC in non-reg, so distribution tax is MTR
                dist_drag = yield_pct * marginal_rate * 10000

        result[acct] = wht_drag + dist_drag

    return result


def asset_location_suggestion(
    etf_type: str,
    marginal_rate: float,
    province: str = "quebec",
) -> list[str]:
    """Suggest optimal account(s) for an ETF by type.

    .. deprecated:: Use :func:`asset_location_tax_impact` instead.
        Per DP#30, the simulator provides tax impact data, not financial advice.
        ``asset_location_tax_impact`` returns structured tax drag data that
        lets the optimizer decide placement.
    """
    import warnings
    warnings.warn(
        "asset_location_suggestion() is deprecated. Use asset_location_tax_impact() "
        "which returns tax drag data per DP#30 instead of prescriptive advice.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Derive suggestion order from tax impact data (lowest drag first)
    tax_impact = asset_location_tax_impact(etf_type, marginal_rate, province)
    return sorted(tax_impact, key=tax_impact.get)
