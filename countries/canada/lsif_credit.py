#!/usr/bin/env python3
"""
LSIF (Labour-Sponsored Investment Fund) Tax Credit Module

Implements federal and Quebec tax credits for LSIF share purchases.
Per DP#10: this module owns LSIF credit rules (ITA s.127.4, TA s.1029.8.5).

Key rules:
- Federal: 15% on first $5,000 of LSIF shares purchased (max $750/year), non-refundable
- Quebec: 15% on first $5,000 of LSIF shares purchased (max $750/year), non-refundable
- Federal phase-out (Bill C-69, Royal Assent June 20, 2024): the federal
  credit for FEDERALLY-registered LSVCCs is eliminated (0%) for shares acquired
  after 2024. Quebec funds (Fonds FTQ, Fondaction) are PROVINCIALLY registered
  and retain the 15% federal credit. Modeled via LSIFPurchase.federally_registered
  (default False) and the year-versioned federal_lsif_rate() (DP#20). See
  federal_lsif_rate() below.
- Federal credit requires a provincial/territorial LSIF program to exist (per ITA s.127.4(1)(b)).
  Per CRA ruling 2003-0006295, Quebec's individual age/retirement restrictions do NOT
  block federal credit — the share is still an "approved share." Note: Fonds FTQ
  practical guidance states federal credit requires Quebec individual eligibility;
  this implementation follows the legally authoritative CRA ruling.
- Quebec credit: age 18-64, Quebec residency, not retired/pension with employment_income ≤ $3,500
  (per CFFP footnote 11, a retired/pension person with employment_income > $3,500 is eligible;
  a non-retired, non-pension person with any employment_income is eligible),
- Starting 2027: no credit if reference-year taxable income exceeds
  the highest Quebec provincial tax bracket (only for shares acquired after 2026-12-31)
- Holding period: based on redemption date (Fonds FTQ amended prospectus April 2024):
  2yr if redeemed before June 1, 2027; 3yr if redeemed June 1 2027–May 31 2029;
  4yr if redeemed June 1 2029–May 31 2031; 5yr if redeemed after May 31 2031
- Interest on borrowed money for LSIF shares is NOT deductible
- Quebec carryforward: any unused credit carries forward
- No federal carryforward exists; the first-60-day rule is about claiming
  timing on the prior-year return, not a carryforward mechanism
- HBP ineligibility: if LSIF shares are HBP replacement shares (ITA s.211.8),
  those shares are ineligible for the credit. This is a boolean ineligibility, not
  a proportional reduction.
- HBP credit recovery: ITA s.211.8 requires that if LSIF shares held in an RRSP
  are withdrawn under the HBP and not repaid within the required timeframe, the
  LSIF credit must be repaid over 8 years (added to income in equal installments).
  See lsif_hbp_credit_recovery() below.

References:
    ITA s.127.4 (federal LSIF credit)
    TA s.1029.8.5 (Quebec LSIF credit)
    https://www.revenuquebec.ca/en/citizens/tax-return/completing-your-tax-return/credits-and-deductions/tax-credit-for-labour-sponsored-funds/
"""

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import List, Optional, Dict, Any

from countries.canada.provinces.quebec.tax_data import QuebecTaxData


# Credit rates and limits
FEDERAL_LSIF_RATE = 0.15
QUEBEC_LSIF_RATE = 0.15
LSIF_PURCHASE_MAX = 5000

# Federal LSIF credit phase-out for FEDERALLY-registered LSVCCs (DP#20, issue #319).
# Bill C-69 (Budget Implementation Act, 2024, No. 1) received Royal Assent on
# June 20, 2024. Per the federal labour-sponsored funds tax credit rules, the
# federal credit for shares of a *federally* prescribed (federally registered)
# LSVCC is eliminated for shares acquired after 2024 (rate 0% for 2025+).
# Quebec funds (Fonds FTQ, Fondaction) are *provincially* registered and keep
# the 15% federal credit, so this schedule applies only when the purchase is
# flagged federally_registered (default False = provincially registered).
#
# The schedule is keyed by acquisition year (DP#20: year-versioned data):
#   <= 2024 → 15%, 2025+ → 0% for federally-registered shares.
# Source: Bill C-69 https://www.parl.ca/legisinfo/en/bill/44-1/c-69
#         https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/lines-413-414-labour-sponsored-funds-tax-credit.html
FEDERAL_REGISTERED_PHASEOUT_LAST_FULL_YEAR = 2024


def federal_lsif_rate(acquisition_year: int, federally_registered: bool) -> float:
    """Federal LSIF credit rate for a share by acquisition year (DP#20).

    Provincially-registered LSVCCs (the Quebec case) keep the 15% federal
    credit. Federally-registered LSVCCs are phased out by Bill C-69: the
    federal credit is eliminated (0%) for shares acquired after 2024.

    Pure function (DP#3): same inputs → same output.
    """
    if not federally_registered:
        return FEDERAL_LSIF_RATE
    if acquisition_year <= FEDERAL_REGISTERED_PHASEOUT_LAST_FULL_YEAR:
        return FEDERAL_LSIF_RATE
    return 0.0

# Eligibility thresholds
EMPLOYMENT_INCOME_MIN = 3500
AGE_MIN = 18
AGE_MAX = 64

# Reference-year income threshold starts in 2027
INCOME_THRESHOLD_START_YEAR = 2027

@lru_cache(maxsize=None)
def qc_highest_provincial_bracket_threshold(reference_year: int) -> float:
    """Highest Quebec provincial tax bracket threshold for the reference year.

    Uses QuebecTaxData provincial brackets (DP#2: data from tax data module,
    not hardcoded). The LSIF income threshold uses the provincial bracket,
    not the combined federal+provincial bracket.
    DP#3: lru_cache preserves purity (internal cache, not module-level mutable state).
    """
    for year_data in QuebecTaxData.all_years():
        if year_data.year == reference_year:
            for bracket in reversed(year_data.provincial_brackets):
                if bracket.max_income == 0:
                    return float(bracket.min_income)
            break
    # Fallback: use closest earlier year
    available = sorted(yd.year for yd in QuebecTaxData.all_years())
    closest = max((y for y in available if y <= reference_year), default=max(available))
    for year_data in QuebecTaxData.all_years():
        if year_data.year == closest:
            for bracket in reversed(year_data.provincial_brackets):
                if bracket.max_income == 0:
                    return float(bracket.min_income)
            break
    raise ValueError(f"No Quebec tax data found for reference year {reference_year}")


def holding_period_years(redemption_date: date) -> int:
    """Holding period required before LSIF redemption without clawback.

    Based on redemption date per Fonds FTQ amended prospectus (April 2024):
    - 2 years if redeemed before June 1, 2027
    - 3 years if redeemed June 1, 2027 – May 31, 2029
    - 4 years if redeemed June 1, 2029 – May 31, 2031
    - 5 years if redeemed after May 31, 2031
    """
    if redemption_date < date(2027, 6, 1):
        return 2
    elif redemption_date < date(2029, 6, 1):
        return 3
    elif redemption_date < date(2031, 6, 1):
        return 4
    else:
        return 5


@dataclass
class LSIFPurchase:
    """An LSIF share purchase with all eligibility context.

    DP#1: stores birth_year (not age); DP#8: data object for simulation.
    """
    amount: float = 0.0
    purchase_year: int = 2026
    birth_year: int = 2000  # DP#13: clearly dated placeholder year
    is_quebec_resident: bool = True
    # DP#28: retirement/pension status is computed from year fields, not stored
    # as booleans. A boolean would go stale when the simulation clock ticks.
    # retirement_year: the year the person retired (None = not retired).
    # pension_start_year: the year the person started receiving pension (None = no pension).
    # Both are subject to the $3,500 employment income exception per TA s.1029.8.5
    # and CFFP footnote 11.
    retirement_year: Optional[int] = None
    pension_start_year: Optional[int] = None
    prior_redemption: bool = False
    # employment_income includes self-employment income ("revenus de travail" per CFFP)
    employment_income: float = 0.0
    reference_year_taxable_income: Optional[float] = None
    quebec_carryforward: float = 0.0  # credit amount (not purchase); added directly, no cap at this level
    is_hbp_replacement: bool = False  # ITA s.211.8: replacement shares, not HBP funds used for purchase
    # DP#20/issue #319: federally-registered LSVCCs are subject to the Bill C-69
    # federal credit phase-out (0% for shares acquired after 2024). Quebec funds
    # (Fonds FTQ, Fondaction) are provincially registered → default False.
    federally_registered: bool = False
    acquisition_date: Optional[date] = None
    redeemed_date: Optional[date] = None

    def acquisition_year(self) -> int:
        """Year the share was acquired (acquisition_date year, else purchase_year)."""
        if self.acquisition_date is not None:
            return self.acquisition_date.year
        return self.purchase_year

    def age_in(self, year: int) -> int:
        """DP#1: compute age from birth_year, don't store it."""
        return year - self.birth_year

    def is_retired_in(self, year: int) -> bool:
        """DP#28: compute retirement status from retirement_year, not a stored boolean.

        A person is retired in any year >= retirement_year.
        Returns False when retirement_year is None (not retired).
        """
        if self.retirement_year is not None:
            return year >= self.retirement_year
        return False

    def receives_pension_in(self, year: int) -> bool:
        """DP#28: compute pension status from pension_start_year, not a stored boolean.

        A person receives a pension in any year >= pension_start_year.
        Returns False when pension_start_year is None (not receiving pension).
        """
        if self.pension_start_year is not None:
            return year >= self.pension_start_year
        return False


@dataclass
class LSIFCreditResult:
    """Result of LSIF credit computation.

    All fields are computed from inputs (DP#3: pure function output).
    """
    federal_credit: float = 0.0
    quebec_credit: float = 0.0
    current_qc_credit: float = 0.0
    quebec_carryforward_used: float = 0.0
    federal_eligible: bool = False
    quebec_eligible: bool = False
    federal_refundable: bool = False
    quebec_refundable: bool = False
    clawback_required: bool = False
    clawback_amount: float = 0.0
    interest_deductible: bool = False
    ineligibility_reasons: List[str] = field(default_factory=list)
    federal_ineligibility_reasons: List[str] = field(default_factory=list)
    quebec_carryforward_preserved: float = 0.0


def _check_federal_eligibility_gates(purchase: LSIFPurchase, year: int) -> List[str]:
    """Return list of reasons the purchase is ineligible for FEDERAL credit.

    Per ITA s.127.4 and CRA ruling 2003-0006295, the federal credit requires
    that the share be an "approved share" — i.e., a provincial/territorial LSIF
    program exists. Quebec's individual age/retirement restrictions do NOT
    constitute "suspension or termination of assistance" per the CRA ruling.
    Only gates that prevent share acquisition entirely block federal credit.
    """
    reasons = []
    age = purchase.age_in(year)
    if age < AGE_MIN:
        reasons.append(f"age {age} < {AGE_MIN}")
    if purchase.prior_redemption:
        reasons.append("prior LSIF redemption")
    if purchase.is_hbp_replacement:
        reasons.append("LSIF shares are HBP replacement shares (ITA s.211.8)")
    return reasons


def _check_eligibility_gates(purchase: LSIFPurchase, year: int) -> List[str]:
    """Return list of reasons the purchase is ineligible for Quebec credit.

    DP#28: eligibility is date-computed from birth_year, not stored.
    NOTE: age < 18, prior_redemption, and is_hbp_replacement checks are
    duplicated in _check_federal_eligibility_gates. Both must be updated together.
    """
    reasons = []
    age = purchase.age_in(year)

    if age < AGE_MIN:
        reasons.append(f"age {age} < {AGE_MIN}")
    if age > AGE_MAX:
        reasons.append(f"age {age} > {AGE_MAX}")
    if not purchase.is_quebec_resident:
        reasons.append("not a Quebec resident")
    # Per TA s.1029.8.5 and CFFP footnote 11: the $3,500 employment income
    # threshold only applies within the retirement/pension context. A non-retired,
    # non-pension-receiving person with employment_income=0 is eligible.
    # DP#28: compute eligibility from year fields, not stored booleans.
    if (purchase.is_retired_in(year) or purchase.receives_pension_in(year)) and purchase.employment_income <= EMPLOYMENT_INCOME_MIN:
        reasons.append(
            f"retired/pension with employment income ${purchase.employment_income:.0f} <= ${EMPLOYMENT_INCOME_MIN}"
        )
    if purchase.prior_redemption:
        reasons.append("prior LSIF redemption")
    if purchase.is_hbp_replacement:
        reasons.append("LSIF shares are HBP replacement shares (ITA s.211.8)")

    # Income threshold starting 2027 (only for shares acquired after 2026-12-31)
    # Reference year is year-2: "deuxième année civile qui précède" (second previous year)
    if year >= INCOME_THRESHOLD_START_YEAR:
        skip_income_threshold = (
            purchase.acquisition_date is not None
            and purchase.acquisition_date <= date(2026, 12, 31)
        )
        if not skip_income_threshold:
            ref_income = purchase.reference_year_taxable_income
            if ref_income is not None:
                threshold = qc_highest_provincial_bracket_threshold(year - 2)
                if ref_income > threshold:
                    reasons.append(
                        f"reference-year income ${ref_income:.0f} > threshold ${threshold:.0f}"
                    )

    return reasons


def compute_lsif_credit(purchase: LSIFPurchase, year: int,
                        provincial_eligible: bool = True) -> LSIFCreditResult:
    """Compute LSIF federal and Quebec tax credits for a purchase.

    Pure function (DP#3): same inputs always produce same outputs.

    Args:
        purchase: LSIF purchase details
        year: Taxation year
        provincial_eligible: Whether a provincial/territorial LSIF credit is
            available. Per ITA s.127.4(1)(b), the federal credit requires that
            a provincial/territorial credit be available. Default True assumes
            Quebec eligibility (the only province with active LSIFs). Set to
            False for non-Quebec residents until other province support is added.
    """
    # Input validation
    if purchase.amount < 0:
        raise ValueError(f"purchase.amount must be non-negative, got {purchase.amount}")

    reasons = _check_eligibility_gates(purchase, year)
    qc_eligible = len(reasons) == 0
    # ITA s.127.4(1)(b): federal credit requires that the province/territory
    # has an LSIF program (the share is an "approved share"). Per CRA ruling
    # 2003-0006295, Quebec's age/retirement/residency restrictions on individuals
    # do NOT constitute "suspension or termination of assistance" — the federal
    # credit is available to anyone acquiring an approved share regardless of
    # Quebec individual eligibility. provincial_eligible=True means a provincial
    # LSIF program exists (default: Quebec has one).
    fed_reasons = _check_federal_eligibility_gates(purchase, year)
    fed_eligible = provincial_eligible and len(fed_reasons) == 0
    if not provincial_eligible:
        fed_reasons = ["no provincial/territorial LSIF program available"] + fed_reasons
    # Bill C-69 (issue #319): federal credit phased out for federally-registered
    # LSVCCs acquired after 2024. Quebec funds are provincially registered (rate
    # unchanged). Year-versioned via federal_lsif_rate (DP#20).
    fed_rate = federal_lsif_rate(purchase.acquisition_year(), purchase.federally_registered)
    if fed_eligible and fed_rate <= 0:
        fed_eligible = False
        fed_reasons = [
            "federal credit phased out for federally-registered LSVCC acquired after 2024 (Bill C-69)"
        ]

    current_qc_credit = 0.0
    qc_credit = 0.0
    fed_credit = 0.0
    carryforward_used = 0.0
    carryforward_preserved = 0.0
    clawback_required = False
    clawback_amount = 0.0

    # Compute Quebec credit (gated by Quebec individual eligibility)
    if qc_eligible:
        eligible_amount = min(purchase.amount, LSIF_PURCHASE_MAX)
        current_qc_credit = eligible_amount * QUEBEC_LSIF_RATE
        # Quebec carryforward: prior-year unused credit added to current year
        # (no federal carryforward exists per ITA s.127.4)
        carryforward_used = purchase.quebec_carryforward
        qc_credit = current_qc_credit + carryforward_used
    else:
        # Per CFFP, Quebec carryforward is indefinitely reportable.
        # When ineligible, preserve the input carryforward for the caller
        # to track across years.
        carryforward_preserved = purchase.quebec_carryforward

    # Compute federal credit (gated by provincial program existence, not QC
    # individual). Rate is year-versioned per Bill C-69 (issue #319).
    if fed_eligible:
        eligible_amount = min(purchase.amount, LSIF_PURCHASE_MAX)
        fed_credit = eligible_amount * fed_rate

    # Holding period clawback: only on current-year credit, NOT carryforward.
    # Per ITA s.127.4 and TA s.1029.8.5, the clawback repays the credit received
    # for the specific shares being redeemed. Carryforward from prior years is
    # not subject to current-year clawback.
    if purchase.redeemed_date is not None:
        holding = holding_period_years(purchase.redeemed_date)
        start = purchase.acquisition_date or date(purchase.purchase_year, 1, 1)
        days_held = (purchase.redeemed_date - start).days
        years_held = days_held / 365.25
        if years_held < holding:
            clawback_required = True
            clawback_amount = current_qc_credit + fed_credit

    return LSIFCreditResult(
        federal_credit=round(fed_credit, 2),
        quebec_credit=round(qc_credit, 2),
        current_qc_credit=round(current_qc_credit, 2),
        quebec_carryforward_used=round(carryforward_used, 2),
        federal_eligible=fed_eligible,
        quebec_eligible=qc_eligible,
        federal_refundable=False,
        quebec_refundable=False,
        clawback_required=clawback_required,
        clawback_amount=round(clawback_amount, 2),
        interest_deductible=False,
        ineligibility_reasons=reasons,
        quebec_carryforward_preserved=round(carryforward_preserved, 2),
        federal_ineligibility_reasons=fed_reasons if not fed_eligible else [],
    )


def lsif_from_config(cfg: Dict[str, Any], birth_year: int, year: int) -> Optional[LSIFPurchase]:
    """Build an LSIFPurchase from input config.

    DP#16: returns None when lsif section is absent (module does not participate).
    """
    lsif = cfg.get("lsif")
    if not lsif:
        return None

    redeemed_date = None
    if lsif.get("redeemed_date"):
        redeemed_date = date.fromisoformat(lsif["redeemed_date"])

    # DP#28: prefer year-based fields over boolean fields for retirement/pension status.
    # retirement_year and pension_start_year are computed on demand.
    # Fall back to deprecated boolean fields for backward compatibility.
    retirement_year = (
        int(lsif["retirement_year"]) if lsif.get("retirement_year") is not None else None
    )
    pension_start_year = (
        int(lsif["pension_start_year"]) if lsif.get("pension_start_year") is not None else None
    )

    return LSIFPurchase(
        amount=float(lsif.get("purchase_amount", 0)),
        purchase_year=int(lsif["purchase_year"]) if lsif.get("purchase_year") else year,
        birth_year=birth_year,
        is_quebec_resident=lsif.get("is_quebec_resident", True),
        retirement_year=retirement_year,
        pension_start_year=pension_start_year,
        prior_redemption=lsif.get("prior_redemption", False),
        employment_income=float(lsif.get("employment_income", 0)),
        reference_year_taxable_income=(
            float(lsif["reference_year_taxable_income"])
            if lsif.get("reference_year_taxable_income") is not None
            else None
        ),
        quebec_carryforward=float(lsif.get("quebec_carryforward", 0)),
        is_hbp_replacement=lsif.get("is_hbp_replacement", False),
        federally_registered=lsif.get("federally_registered", False),
        acquisition_date=(
            date.fromisoformat(lsif["acquisition_date"])
            if lsif.get("acquisition_date")
            else None
        ),
        redeemed_date=redeemed_date,
    )


def lsif_hbp_credit_recovery(
    lsif_credit_amount: float,
    hbp_withdrawal_year: int,
    recovery_start_year: int,
    recovery_years: int = 8,
) -> Dict[int, float]:
    """Compute annual LSIF credit recovery under ITA s.211.8 when HBP is not repaid.

    When LSIF shares withdrawn under the HBP are not repaid within the required
    timeframe, the LSIF credit claimed on those shares must be repaid (added to
    income) in equal installments over 8 years (ITA s.211.8).

    Pure function (DP#3): same inputs → same output.

    Args:
        lsif_credit_amount: Total federal LSIF credit claimed on the withdrawn shares
        hbp_withdrawal_year: Year the HBP withdrawal was made
        recovery_start_year: Year the 8-year recovery period begins
            (typically 2 years after the HBP withdrawal year, since HBP allows
            up to 2 years before repayment must start)
        recovery_years: Number of years for recovery (default 8 per ITA s.211.8)

    Returns:
        Dict mapping each recovery year to the annual recovery amount.
        The final year gets the remainder to handle rounding.
    """
    if lsif_credit_amount <= 0:
        return {}
    if recovery_years <= 0:
        return {}

    annual_amount = lsif_credit_amount / recovery_years
    # Round annual amounts to cents, put remainder in final year
    annual_rounded = round(annual_amount, 2)
    total_allocated = annual_rounded * (recovery_years - 1)
    final_amount = round(lsif_credit_amount - total_allocated, 2)

    recovery_schedule = {}
    for i in range(recovery_years):
        year = recovery_start_year + i
        if i < recovery_years - 1:
            recovery_schedule[year] = annual_rounded
        else:
            recovery_schedule[year] = final_amount
    return recovery_schedule