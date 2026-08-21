#!/usr/bin/env python3
"""
CPP/QPP Sharing — Service Canada Pension Sharing Rules

This module models CPP (Canada Pension Plan) and QPP (Quebec Pension Plan)
sharing between spouses/common-law partners.

Per DP#10: this module owns CPP/QPP sharing rules (Service Canada program).
Per DP#6: sharing is discovered from eligibility conditions, not named.
Per DP#3: all functions are pure — same inputs → same outputs.
Per DP#1: dates/ages are stored, derived values (eligibility, sharing ratio)
are computed on demand.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — CPP/QPP sharing entry and CPP Enhancement/CPP2 entry
    Service Canada CPP sharing: https://www.canada.ca/en/services/benefits/publicpensions/cpp/share-cpp.html
    Retraite Québec (QPP): https://www.retr.quebec.ca/en
    CPP2 contribution rates: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
    CPP enhancement overview: https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-enhancement.html

Key Rules (Service Canada):
- Both spouses must be receiving CPP/QPP retirement pension
- Both must be at least 60 years old
- They must be living together (cohabiting)
- Sharing is based on the proportion of months lived together during
  the joint contributory period
- Post-retirement benefits (earned after starting CPP) are NOT shareable
- The combined total benefit does NOT increase — it's redistributed
- CPP sharing is separate from pension income splitting (T1032)
- Quebec residents: QPP follows similar rules with Quebec-specific calculations

SCENARIO 12.2: CPP sharing tax benefit calculation.
SCENARIO 12.1: Combined CPP sharing + pension splitting.

Usage:
    from countries.canada.cpp_sharing import (
        CPPSharingInput, cpp_sharing_eligibility,
        compute_sharing_ratio, compute_shared_benefits,
        cpp_sharing_tax_benefit, optimize_cpp_sharing,
        compute_cpp2_contribution, compute_cpp2_benefit,
        compute_survivor_benefit,
    )

    input_data = CPPSharingInput(
        primary_birth_year=2000,  # DP#13: clearly dated placeholder, not a real person
        spouse_birth_year=2002,  # DP#13: clearly dated placeholder, not a real person
        primary_cpp_annual=15000,
        spouse_cpp_annual=8000,
        cohabitation_start_year=2025,
    )
    result = optimize_cpp_sharing(input_data)
    print(f"Monthly tax savings: ${result.monthly_tax_savings:.0f}")
"""

from tax_data import default_tax_provider
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


# =============================================================================
# Constants — Service Canada / Retraite Québec rules (2026 defaults, DP#13)
# =============================================================================

CPP_EARLIEST_START_AGE = 60
CPP_STANDARD_AGE = 65
CPP_LATEST_START_AGE = 70
CPP_MAX_PENSIONABLE_2026 = 74600       # YMPE 2026 (Yearly Maximum Pensionable Earnings) — DEPRECATED: use TaxDataProvider (DP#20)
CPP_MAX_BENEFIT_65_2026 = 18092        # Maximum CPP retirement pension at 65: $1,507.65 × 12 — DEPRECATED: use TaxDataProvider (DP#20)
CPP2_MAX_PENSIONABLE_2026 = 81900     # YMPE2 for CPP2 (2026) — DEPRECATED: use TaxDataProvider.get_cpp2_max_pensionable(year) (DP#20, DP#12)
CPP_BASIC_EXEMPTION = 3500            # Basic exemption
CPP_EARLY_PENALTY_PER_MONTH = 0.006   # 0.6% per month before 65
CPP_LATE_BONUS_PER_MONTH = 0.007      # 0.7% per month after 65

# QPP 2026 parameters (same framework, Quebec-administered)
QPP_MAX_PENSIONABLE_2026 = 74600      # Same YMPE — DEPRECATED: use TaxDataProvider (DP#20)
QPP_MAX_BENEFIT_65_2026 = 17334       # QPP max benefit at 65 (2026) — DEPRECATED: use TaxDataProvider (DP#52)

# ── Contribution rates (2026 defaults, DP#13, DP#52) ──
CPP_RATE_2026 = 0.0595               # CPP employee contribution rate 5.95%
QPP_RATE_2026 = 0.0640               # QPP employee contribution rate 6.40% (higher than CPP)
CPP2_RATE = 0.04                     # CPP2/QPP2 employee rate 4% (since 2024)
CPP2_SELF_EMPLOYED_RATE = 0.08       # CPP2/QPP2 self-employed rate 8%

# ── CPP2 benefit accrual rate (DP#52) ──
CPP2_ACCRUAL_RATE = 1 / 40          # Same 1/40 accrual as enhanced CPP (2019+)

# ── Survivor benefit rates (DP#52) ──
CPP_SURVIVOR_RATE_65_PLUS = 0.60     # 60% of deceased's CPP for survivor age 65+
CPP_SURVIVOR_RATE_UNDER_65 = 0.375   # 37.5% of deceased's CPP for survivor under 65
QPP_SURVIVOR_RATE = 0.375            # QPP: 37.5% of deceased's QPP + flat rate


# =============================================================================
# Data Classes — DP#8: Compose through data, not inheritance
# =============================================================================

class Province(Enum):
    """Canadian provinces/territories for CPP/QPP determination."""
    QUEBEC = "quebec"
    ONTARIO = "ontario"
    OTHER = "other"  # All other provinces/territories use CPP

    @property
    def is_quebec(self) -> bool:
        return self == Province.QUEBEC

    @property
    def pension_plan(self) -> str:
        """Which pension plan applies."""
        return "QPP" if self.is_quebec else "CPP"


@dataclass
class CPPSharingInput:
    """Input data for CPP/QPP sharing calculation.

    Per DP#1: birth years, not ages. Cohabitation start year, not duration.
    Per DP#4: role-based identifiers (primary, spouse).
    Per DP#15: all data is fake round numbers in tests; real data in input.json.
    """
    # Per DP#1: store dates, not derived values.
    # Per DP#13/#32: no person-specific default. `0` is a sentinel, not a
    # birth year -- primary_age_in()/spouse_age_in() and compute_sharing_ratio()
    # raise loudly on it rather than simulating a plausible person (issue #741).
    primary_birth_year: int = 0  # DP#1; DP#13: no person-specific defaults
    spouse_birth_year: int = 0   # DP#1; DP#13: no person-specific defaults

    # CPP benefits — user provides these as data (DP#30: we don't predict benefits)
    primary_cpp_annual: float = 15000.0
    spouse_cpp_annual: float = 8000.0

    # Post-retirement benefits (earned after starting CPP, NOT shareable)
    primary_prb_annual: float = 0.0
    spouse_prb_annual: float = 0.0

    # CPP start ages (default 65 if not specified)
    primary_start_age: int = 65
    spouse_start_age: int = 65

    # Cohabitation history (DP#1: dates, not duration)
    cohabitation_start_year: int = 1985
    cohabitation_end_year: Optional[int] = None  # None = still cohabiting

    # Contributory period info
    primary_contribution_start_year: int = 1978  # Age 18 or first year worked
    spouse_contribution_start_year: int = 1980

    # Province determines CPP vs QPP (DP#10)
    province: str = "quebec"

    # Other income (not CPP) for MTR calculation (DP#2: injected, not hardcoded)
    primary_other_income: float = 0.0
    spouse_other_income: float = 0.0

    # Year for calculation (allows year-specific data, DP#20)
    calculation_year: int = 2026

    # Tax brackets for MTR calculation (injected, DP#2)
    brackets: Optional[List[Dict]] = None

    def primary_age_in(self, year: int) -> int:
        """Per DP#1: compute age from birth year.

        Per DP#32: a sentinel ``primary_birth_year == 0`` (the default when a
        caller omits it) must fail loudly, not yield a 2026-year-old -- issue
        #741. Mirrors ``LockedInAccount.age_in`` in ``locked_in_account.py``.
        """
        age = year - self.primary_birth_year
        if self.primary_birth_year == 0 or age < 0 or age > 130:
            raise ValueError(
                f"Invalid primary_birth_year={self.primary_birth_year}: "
                f"primary_age_in({year})={age}. primary_birth_year must be a "
                f"valid year of birth (DP#1/DP#32, #741)."
            )
        return age

    def spouse_age_in(self, year: int) -> int:
        """Per DP#1: compute age from birth year.

        Per DP#32: a sentinel ``spouse_birth_year == 0`` (the default when a
        caller omits it) must fail loudly, not yield a 2026-year-old -- issue
        #741. Mirrors ``LockedInAccount.age_in`` in ``locked_in_account.py``.
        """
        age = year - self.spouse_birth_year
        if self.spouse_birth_year == 0 or age < 0 or age > 130:
            raise ValueError(
                f"Invalid spouse_birth_year={self.spouse_birth_year}: "
                f"spouse_age_in({year})={age}. spouse_birth_year must be a "
                f"valid year of birth (DP#1/DP#32, #741)."
            )
        return age

    @property
    def province_enum(self) -> Province:
        """Convert string province to enum."""
        province_map = {
            "quebec": Province.QUEBEC,
            "qc": Province.QUEBEC,
            "ontario": Province.ONTARIO,
            "on": Province.ONTARIO,
        }
        return province_map.get(self.province.lower(), Province.OTHER)

    @property
    def primary_shareable_cpp(self) -> float:
        """CPP benefit that IS shareable (total minus post-retirement benefits)."""
        return max(0, self.primary_cpp_annual - self.primary_prb_annual)

    @property
    def spouse_shareable_cpp(self) -> float:
        """CPP benefit that IS shareable (total minus post-retirement benefits)."""
        return max(0, self.spouse_cpp_annual - self.spouse_prb_annual)


@dataclass
class CPPSharingResult:
    """Result of CPP/QPP sharing calculation.

    Attributes:
        eligible: Whether CPP sharing is available
        sharing_ratio: Portion of contributory period cohabiting (0-1)
        primary_shared_amount: Amount transferred from primary to spouse
        spouse_shared_amount: Amount transferred from spouse to primary (net effect)
        primary_total_after_sharing: Primary's total CPP after sharing
        spouse_total_after_sharing: Spouse's total CPP after sharing
        primary_prb_preserved: Primary's PRB (unchanged by sharing)
        spouse_prb_preserved: Spouse's PRB (unchanged by sharing)
        combined_total_preserved: Combined total should equal combined before
        monthly_tax_savings: Estimated monthly family tax savings
        annual_tax_savings: Estimated annual family tax savings
        eligibility_reasons: List of eligibility check results
    """
    eligible: bool = False
    sharing_ratio: float = 0.0
    primary_shared_amount: float = 0.0
    spouse_shared_amount: float = 0.0
    primary_total_after_sharing: float = 0.0
    spouse_total_after_sharing: float = 0.0
    primary_prb_preserved: float = 0.0
    spouse_prb_preserved: float = 0.0
    combined_total_preserved: float = 0.0
    monthly_tax_savings: float = 0.0
    annual_tax_savings: float = 0.0
    eligibility_reasons: List[str] = field(default_factory=list)


# =============================================================================
# Pure Functions — CPP/QPP Sharing Rules
# =============================================================================

def cpp_sharing_eligibility(data: CPPSharingInput) -> Dict[str, bool]:
    """Check all CPP sharing eligibility conditions.

    Per DP#28: eligibility is a date-computed gate. Programs enter and exit
    on a schedule defined by dates/ages.

    Conditions (Service Canada):
    1. Both spouses/common-law partners receiving CPP/QPP retirement pension
    2. Both at least 60 years old (can share if one starts at 60)
    3. Living together (cohabiting)
    4. Both have made at least one CPP/QPP contribution

    Args:
        data: CPPSharingInput with birth years, CPP amounts, cohabitation info

    Returns:
        Dict with each condition and overall eligible flag
    """
    year = data.calculation_year
    primary_age = data.primary_age_in(year)
    spouse_age = data.spouse_age_in(year)

    conditions = {
        "both_receiving_cpp": (
            data.primary_cpp_annual > 0 and data.spouse_cpp_annual > 0
        ),
        "both_at_least_60": primary_age >= 60 and spouse_age >= 60,
        "cohabiting": data.cohabitation_end_year is None,
        "primary_has_contributions": data.primary_contribution_start_year > 0,
        "spouse_has_contributions": data.spouse_contribution_start_year > 0,
    }

    conditions["eligible"] = all(conditions.values())
    conditions["primary_age"] = primary_age
    conditions["spouse_age"] = spouse_age

    if not conditions["eligible"]:
        reasons = []
        if not conditions["both_receiving_cpp"]:
            reasons.append("Both spouses must be receiving CPP/QPP")
        if not conditions["both_at_least_60"]:
            if primary_age < 60:
                reasons.append(f"Primary is {primary_age}, must be 60+")
            if spouse_age < 60:
                reasons.append(f"Spouse is {spouse_age}, must be 60+")
        if not conditions["cohabiting"]:
            reasons.append("Spouses must be cohabiting (living together)")
        conditions["reasons"] = reasons

    return conditions


def compute_sharing_ratio(data: CPPSharingInput) -> float:
    """Compute the CPP sharing ratio based on cohabitation during contributory period.

    Per DP#3: pure function. Same inputs always produce the same ratio.

    The sharing ratio is the proportion of the joint contributory period
    during which the spouses lived together. This determines how much
    of each person's CPP is pooled for redistribution.

    For example:
    - If primary contributed 1980-2026 (46 years) and spouse 1982-2026 (44 years)
    - Joint contributory period: 1982-2026 (44 years = 528 months)
    - If cohabiting since 1985: 1985-2026 (41 years = 492 months)
    - Sharing ratio: 492/528 ≈ 0.932

    Only the shareable pool (total CPP minus PRB) is redistributed.
    Post-retirement benefits are excluded per CRA rules.

    Args:
        data: CPPSharingInput with cohabitation and contribution dates

    Returns:
        Sharing ratio (0.0 to 1.0)
    """
    # Contributory period for each person: age 18 to start of CPP or age 65
    # whichever comes first, or to calculation year if still contributing
    year = data.calculation_year

    # DP#32/#741: birth_year drives the contributory-period end computation
    # below; a sentinel 0 (the default when a caller omits it) must fail
    # loudly here rather than produce a confident ratio for the wrong person.
    data.primary_age_in(year)
    data.spouse_age_in(year)

    primary_contrib_start = data.primary_contribution_start_year
    spouse_contrib_start = data.spouse_contribution_start_year

    # End of contributory period: start age or calculation year
    primary_end = min(
        data.primary_birth_year + data.primary_start_age,
        year
    )
    spouse_end = min(
        data.spouse_birth_year + data.spouse_start_age,
        year
    )

    # Joint contributory period: overlap of both contribution periods
    joint_start = max(primary_contrib_start, spouse_contrib_start)
    joint_end = min(primary_end, spouse_end)

    if joint_start >= joint_end:
        # No overlap in contributory periods
        return 0.0

    joint_months = (joint_end - joint_start) * 12

    # Cohabitation during joint period
    cohab_start = data.cohabitation_start_year
    cohab_end = data.cohabitation_end_year or year  # None means still together

    # Cohabitation overlap with joint contributory period
    cohab_in_joint_start = max(cohab_start, joint_start)
    cohab_in_joint_end = min(cohab_end, joint_end)

    if cohab_in_joint_start >= cohab_in_joint_end:
        # No cohabitation during joint period
        return 0.0

    cohab_months = (cohab_in_joint_end - cohab_in_joint_start) * 12

    ratio = cohab_months / joint_months if joint_months > 0 else 0.0
    return min(1.0, max(0.0, ratio))


def compute_shared_benefits(data: CPPSharingInput) -> CPPSharingResult:
    """Compute CPP/QPP shared benefit amounts.

    Per DP#3: pure function. Same inputs → same outputs.

    The combined shareable CPP is pooled and redistributed based on
    the sharing ratio. Post-retirement benefits are NOT shared.

    Formula:
    - shareable_pool = primary_shareable + spouse_shareable
    - shared_amount = shareable_pool × sharing_ratio / 2
    - primary_after = (primary_shareable - primary_shareable × sharing_ratio)
                     + shared_amount
                     + primary_prb
    - Actually: each person's shareable portion is split:
      primary gets (1 - ratio) × primary_shareable + ratio × spouse_shareable share
      ...simplified to: pool is averaged proportionally

    Service Canada method:
    - Pool the shareable portions based on sharing ratio
    - Redistribute equally from the pool
    - Each person keeps their own PRB

    Args:
        data: CPPSharingInput with all required fields

    Returns:
        CPPSharingResult with shared benefit amounts
    """
    eligibility = cpp_sharing_eligibility(data)

    if not eligibility["eligible"]:
        reasons = eligibility.get("reasons", ["Not eligible for CPP sharing"])
        return CPPSharingResult(
            eligible=False,
            primary_total_after_sharing=data.primary_cpp_annual,
            spouse_total_after_sharing=data.spouse_cpp_annual,
            primary_prb_preserved=data.primary_prb_annual,
            spouse_prb_preserved=data.spouse_prb_annual,
            combined_total_preserved=data.primary_cpp_annual + data.spouse_cpp_annual,
            eligibility_reasons=reasons,
        )

    ratio = compute_sharing_ratio(data)

    primary_shareable = data.primary_shareable_cpp
    spouse_shareable = data.spouse_shareable_cpp
    total_shareable = primary_shareable + spouse_shareable

    # Each person's shareable CPP is split based on the sharing ratio
    # The "shared pool" = ratio × total_shareable
    # This pool is divided equally between the two spouses
    # Each person retains (1 - ratio) of their own shareable benefit
    # Plus receives half the shared pool

    shared_pool = ratio * total_shareable

    # Primary keeps (1 - ratio) of their shareable, plus half the shared pool
    primary_retained_own = (1 - ratio) * primary_shareable
    primary_share_of_pool = shared_pool / 2

    # Spouse keeps (1 - ratio) of their shareable, plus half the shared pool
    spouse_retained_own = (1 - ratio) * spouse_shareable
    spouse_share_of_pool = shared_pool / 2

    # Total including PRB (which is NOT shared, just preserved)
    primary_total = primary_retained_own + primary_share_of_pool + data.primary_prb_annual
    spouse_total = spouse_retained_own + spouse_share_of_pool + data.spouse_prb_annual

    # Net transfer amounts (from the shareable portion only)
    primary_shareable_transfer = primary_share_of_pool - ratio * primary_shareable
    # This is negative if primary is losing shareable CPP, positive if gaining
    primary_net_transfer = primary_total - data.primary_cpp_annual
    # Positive means primary gained; negative means primary lost to spouse

    return CPPSharingResult(
        eligible=True,
        sharing_ratio=ratio,
        primary_shared_amount=primary_share_of_pool - ratio * primary_shareable,
        spouse_shared_amount=spouse_share_of_pool - ratio * spouse_shareable,
        primary_total_after_sharing=primary_total,
        spouse_total_after_sharing=spouse_total,
        primary_prb_preserved=data.primary_prb_annual,
        spouse_prb_preserved=data.spouse_prb_annual,
        combined_total_preserved=primary_total + spouse_total,
        eligibility_reasons=["Eligible for CPP sharing"],
    )


def share_cpp_amounts(
    primary_cpp: float,
    spouse_cpp: float,
    share: float,
) -> Tuple[float, float]:
    """Apply an ELECTED CPP/QPP sharing as a pure transfer toward equalization.

    CPP/QPP sharing (Service Canada application) redistributes the two spouses'
    shareable retirement pensions toward EQUAL amounts; the combined total is
    never increased, only re-split (see ``compute_shared_benefits`` for the
    full date-derived cohabitation-ratio version). This is the MECHANISM the
    live retirement fold calls: ``share`` is the elected fraction of the way to
    full equalization (0 = no sharing, 1 = fully equalized), and the household
    total is CONSERVED. The tax consequence — moving CPP off the higher-bracket
    spouse's stack onto the lower-bracket spouse's — is priced downstream by the
    per-spouse progressive drawdown, not here (DP#30: model the consequence of
    the election; DP#22: ``share`` is the election the optimizer sweeps).

    Per DP#3: pure. Per DP#32: ``share == 0`` is a real value ("no sharing
    elected") returning the inputs unchanged, never coerced.

    Args:
        primary_cpp: primary spouse's CPP/QPP retirement pension.
        spouse_cpp: spouse's CPP/QPP retirement pension.
        share: fraction (0..1) of the way from the current split to a fully
            equalized 50/50 split.

    Returns:
        ``(new_primary_cpp, new_spouse_cpp)`` — the household total is conserved
        (``new_primary + new_spouse == primary_cpp + spouse_cpp``).
    """
    if share < 0 or share > 1:
        raise ValueError(
            f"share={share} is outside [0, 1]: it is the fraction of the way "
            "to a fully equalized CPP split, not a dollar amount."
        )
    mean = (primary_cpp + spouse_cpp) / 2.0
    new_primary = primary_cpp + share * (mean - primary_cpp)
    new_spouse = spouse_cpp + share * (mean - spouse_cpp)
    return new_primary, new_spouse


def cpp_sharing_tax_benefit(
    data: CPPSharingInput,
    brackets: Optional[List[Dict]] = None,
) -> Dict:
    """Compute the tax benefit of CPP sharing.

    Per DP#30: this models the tax consequences of CPP sharing, not
    whether you should apply for it.

    The tax benefit comes from shifting CPP income from the higher-MTR
    spouse to the lower-MTR spouse. The total family tax is reduced by:
        tax_savings = transferred_amount × (primary_MTR - spouse_MTR)

    This is separate from pension income splitting (T1032) and the two
    can be combined (SCENARIO 12.2).

    Args:
        data: CPPSharingInput
        brackets: Tax brackets for MTR calculation (default: load from tax_data)

    Returns:
        Dict with tax savings breakdown
    """
    from tax_calculator import marginal_rate

    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets()

    result = compute_shared_benefits(data)

    if not result.eligible:
        return {
            "eligible": False,
            "reasons": result.eligibility_reasons,
            "annual_tax_savings": 0,
            "monthly_tax_savings": 0,
            "primary_mtr": 0,
            "spouse_mtr": 0,
            "net_transfer": 0,
        }

    # Compute MTRs based on total income INCLUDING CPP
    # The MTR at total income determines the tax cost of each CPP dollar
    primary_income = data.primary_other_income + data.primary_cpp_annual
    spouse_income = data.spouse_other_income + data.spouse_cpp_annual

    primary_mtr = marginal_rate(primary_income, brackets)
    spouse_mtr = marginal_rate(spouse_income, brackets)

    # Net transfer: amount moved from higher-MTR to lower-MTR spouse
    net_transfer_from_primary = (
        result.primary_total_after_sharing - data.primary_cpp_annual
    )
    # Positive means primary GAINED income (net transfer to primary from spouse)
    # Negative means primary LOST income (net transfer from primary to spouse)

    if net_transfer_from_primary < 0:
        # Primary lost CPP income to spouse — tax savings at MTR gap
        transfer_amount = -net_transfer_from_primary
        tax_savings = transfer_amount * (primary_mtr - spouse_mtr)
    elif net_transfer_from_primary > 0:
        # Primary gained CPP income from spouse
        transfer_amount = net_transfer_from_primary
        tax_savings = transfer_amount * (spouse_mtr - primary_mtr)
    else:
        tax_savings = 0

    # Tax savings cannot be negative (sharing never increases tax)
    tax_savings = max(0, tax_savings)

    return {
        "eligible": True,
        "sharing_ratio": result.sharing_ratio,
        "primary_cpp_before": data.primary_cpp_annual,
        "primary_cpp_after": result.primary_total_after_sharing,
        "spouse_cpp_before": data.spouse_cpp_annual,
        "spouse_cpp_after": result.spouse_total_after_sharing,
        "net_transfer_from_primary": net_transfer_from_primary,
        "primary_mtr": primary_mtr,
        "spouse_mtr": spouse_mtr,
        "mtr_gap": abs(primary_mtr - spouse_mtr),
        "annual_tax_savings": tax_savings,
        "monthly_tax_savings": tax_savings / 12,
        "combined_total_preserved": result.combined_total_preserved,
        "province": data.province,
        "pension_plan": data.province_enum.pension_plan,
    }


def optimize_cpp_sharing(
    data: CPPSharingInput,
    brackets: Optional[List[Dict]] = None,
) -> Dict:
    """Optimize CPP sharing — decide whether to share and how much.

    Per DP#22: the optimizer ranks, it doesn't choose. It returns
    both the sharing result and the no-sharing result so the user
    can compare.

    If the MTR gap is zero (equal incomes), CPP sharing has no
    tax benefit. If there's a gap, sharing reduces family tax.

    Note: CPP sharing is all-or-nothing — you can't partially share.
    You either apply for sharing or you don't. The sharing ratio is
    determined by your cohabitation history, not by choice.

    Args:
        data: CPPSharingInput
        brackets: Tax brackets for MTR calculation

    Returns:
        Dict with comparison of sharing vs no-sharing
    """
    benefit = cpp_sharing_tax_benefit(data, brackets)

    if not benefit["eligible"]:
        return {
            "recommendation": "not_eligible",
            "reasons": benefit.get("reasons", []),
            "tax_savings": 0,
            "sharing_result": None,
        }

    # Compute no-sharing baseline using MTR at total income
    from tax_calculator import marginal_rate, tax_on_income
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets()

    primary_income = data.primary_other_income + data.primary_cpp_annual
    spouse_income = data.spouse_other_income + data.spouse_cpp_annual

    # Total tax without sharing
    primary_tax_no_sharing = tax_on_income(primary_income, brackets) \
        - tax_on_income(data.primary_other_income, brackets)
    spouse_tax_no_sharing = tax_on_income(spouse_income, brackets) \
        - tax_on_income(data.spouse_other_income, brackets)
    total_tax_no_sharing = primary_tax_no_sharing + spouse_tax_no_sharing

    # Total tax with sharing
    primary_tax_sharing = tax_on_income(
        data.primary_other_income + benefit["primary_cpp_after"], brackets
    ) - tax_on_income(data.primary_other_income, brackets)
    spouse_tax_sharing = tax_on_income(
        data.spouse_other_income + benefit["spouse_cpp_after"], brackets
    ) - tax_on_income(data.spouse_other_income, brackets)
    total_tax_sharing = primary_tax_sharing + spouse_tax_sharing

    annual_savings = total_tax_no_sharing - total_tax_sharing
    # Sharing should never increase tax, but numerical edge cases
    annual_savings = max(0, annual_savings)

    recommendation = "share" if annual_savings > 0 else "no_benefit"

    return {
        "recommendation": recommendation,
        "sharing_ratio": benefit["sharing_ratio"],
        "primary_cpp_before": data.primary_cpp_annual,
        "primary_cpp_after": benefit["primary_cpp_after"],
        "spouse_cpp_before": data.spouse_cpp_annual,
        "spouse_cpp_after": benefit["spouse_cpp_after"],
        "primary_mtr": benefit["primary_mtr"],
        "spouse_mtr": benefit["spouse_mtr"],
        "mtr_gap": benefit["mtr_gap"],
        "annual_tax_savings": annual_savings,
        "monthly_tax_savings": annual_savings / 12,
        "net_transfer_from_primary": benefit["net_transfer_from_primary"],
        "combined_total_preserved": benefit["combined_total_preserved"],
        "pension_plan": benefit["pension_plan"],
        # For comparison with pension splitting
        "separate_from_pension_splitting": True,
        "note": (
            "CPP sharing is done through Service Canada, not on your tax return. "
            "It is separate from and can be combined with pension income "
            "splitting (T1032)."
        ),
    }


def combined_cpp_and_pension_split(
    data: CPPSharingInput,
    eligible_pension: float = 0,
    spouse_a_other_income: float = 0,
    spouse_b_other_income: float = 0,
    spouse_a_age: Optional[int] = None,
    spouse_b_age: Optional[int] = None,
    province: Optional[str] = None,
) -> Dict:
    """Compute combined benefit of CPP sharing + pension income splitting.

    SCENARIO 12.2: Combined CPP sharing and pension splitting.

    These are separate programs:
    - CPP sharing: Service Canada application, redistributes CPP benefits
    - Pension splitting: T1032 election on tax return, splits eligible pension income

    They work independently and can both be used. The combined benefit
    is the sum of each benefit (they don't interfere with each other).

    Per DP#25: this composes modules (cpp_sharing + pension_split_optimizer)
    without creating dependencies between them.

    Args:
        data: CPPSharingInput for CPP sharing calculation
        eligible_pension: Amount of pension income eligible for splitting (RRIF, etc.)
        spouse_a_other_income: Primary's non-CPP, non-pension income
        spouse_b_other_income: Spouse's non-CPP, non-pension income
        spouse_a_age: Primary's age (defaults from data)
        spouse_b_age: Spouse's age (defaults from data)
        province: Province code (defaults from data)

    Returns:
        Dict with combined benefit breakdown
    """
    from countries.canada.pension_split_optimizer import optimize_pension_split

    brackets = default_tax_provider().get_combined_brackets()
    if data.brackets:
        brackets = data.brackets

    # Use age from data if not specified
    if spouse_a_age is None:
        spouse_a_age = data.primary_age_in(data.calculation_year)
    if spouse_b_age is None:
        spouse_b_age = data.spouse_age_in(data.calculation_year)
    if province is None:
        province = data.province

    # Step 1: CPP sharing benefit
    cpp_result = optimize_cpp_sharing(data, brackets)

    # Step 2: Pension splitting benefit (on RRIF/eligible pension, not CPP)
    # CPP income is NOT eligible for pension splitting — only RRIF/LIF/pension
    # So pension splitting applies to eligible_pension only
    pension_split_result = None
    if eligible_pension > 0 and spouse_a_age >= 65:
        pension_split_result = optimize_pension_split(
            spouse_a_income=spouse_a_other_income + cpp_result.get("primary_cpp_after", data.primary_cpp_annual),
            spouse_b_income=spouse_b_other_income + cpp_result.get("spouse_cpp_after", data.spouse_cpp_annual),
            eligible_pension=eligible_pension,
            spouse_a_age=spouse_a_age,
            spouse_b_age=spouse_b_age,
            province=province,
            brackets=brackets,
        )

    # Combined savings
    cpp_savings = cpp_result.get("annual_tax_savings", 0)
    pension_savings = pension_split_result.tax_savings if pension_split_result else 0
    combined_savings = cpp_savings + pension_savings

    return {
        "cpp_sharing": cpp_result,
        "pension_splitting": {
            "optimal_split_pct": pension_split_result.optimal_split_pct if pension_split_result else 0,
            "optimal_split_amount": pension_split_result.optimal_split_amount if pension_split_result else 0,
            "tax_savings": pension_savings,
            "oas_savings": pension_split_result.oas_savings if pension_split_result else 0,
        } if pension_split_result else None,
        "combined_annual_savings": combined_savings,
        "combined_monthly_savings": combined_savings / 12,
        "programs_are_independent": True,
        "note": (
            "CPP sharing and pension income splitting are separate programs. "
            "CPP sharing is done through Service Canada; pension splitting is "
            "done on your tax return (T1032). Both can be used simultaneously."
        ),
    }


# =============================================================================
# CPP2/QPP2 Contribution and Benefit Calculations (DP#52, issue #52)
# =============================================================================

def compute_cpp2_contribution(
    employment_income: float,
    year: int = 2026,
    province: str = "ontario",
    provider: Optional['TaxDataProvider'] = None,
) -> Dict:
    """Compute CPP/QPP and CPP2/QPP2 contributions for employment income.

    Per DP#52: models both the standard CPP1/QPP1 tier and the second
    additional CPP2/QPP2 tier introduced in 2024.

    CPP1: (min(income, YMPE) - basic_exemption) × rate
    CPP2: (min(income, YAMPE) - YMPE) × 4%   [no basic exemption]
    Total employee = CPP1_employee + CPP2_employee
    Self-employed = 2 × employee (both portions)

    QPP uses the same YMPE/YAMPE/basic_exemption but has a higher
    contribution rate (6.40% vs 5.95% for 2026). QPP2 rate equals
    CPP2 rate (4% employee, 8% self-employed).

    Per DP#20: all parameters are year-versioned via TaxDataProvider.
    Per DP#3: pure function — same inputs always produce same outputs.

    References:
        CPP2 rates: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
        QPP rates: https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/quebec-pension-plan-qpp/contributions/

    Args:
        employment_income: Annual employment income (gross)
        year: Taxation year for year-versioned data (DP#20)
        province: Province code (determines CPP vs QPP rates)
        provider: Optional TaxDataProvider override

    Returns:
        Dict with cpp1_employee, cpp2_employee, total_employee,
        cpp1_self_employed, cpp2_self_employed, total_self_employed,
        ympe, yampe, rate, cpp2_rate, province
    """
    from tax_data import TaxDataProvider as _TDP

    if provider is None:
        provider = _TDP()

    is_quebec = province.lower() in ("quebec", "qc")

    # Load year-versioned data (DP#20)
    # Try province-specific data first (it has provincial CPP/QPP params)
    # Fall back to federal data if province data not available
    try:
        data = provider.get_year_data(year, "canada", province.lower())
    except (ValueError, KeyError):
        data = provider.get_year_data(year, "canada", "federal")

    ympe = data.cpp_max_pensionable
    yampe = data.cpp2_max_pensionable
    basic_exemption = data.cpp_exemption
    cpp2_rate = data.cpp2_rate

    # QPP uses higher rate; CPP uses standard rate
    if is_quebec and data.qpp_rate > 0:
        cpp1_rate = data.qpp_rate
    else:
        cpp1_rate = data.cpp_rate

    # --- CPP1/QPP1 contribution ---
    pensionable_earnings = max(0, min(employment_income, ympe) - basic_exemption)
    cpp1_employee = pensionable_earnings * cpp1_rate
    cpp1_self_employed = cpp1_employee * 2  # Both portions

    # --- CPP2/QPP2 contribution (earnings between YMPE and YAMPE) ---
    # No basic exemption for CPP2 — all earnings in this band are subject
    if yampe > ympe and employment_income > ympe:
        cpp2_earnings = min(employment_income, yampe) - ympe
        cpp2_employee = cpp2_earnings * cpp2_rate
    else:
        cpp2_employee = 0.0
    cpp2_self_employed = cpp2_employee * 2  # Both portions

    total_employee = cpp1_employee + cpp2_employee
    total_self_employed = cpp1_self_employed + cpp2_self_employed

    return {
        "cpp1_employee": round(cpp1_employee, 2),
        "cpp2_employee": round(cpp2_employee, 2),
        "total_employee": round(total_employee, 2),
        "cpp1_self_employed": round(cpp1_self_employed, 2),
        "cpp2_self_employed": round(cpp2_self_employed, 2),
        "total_self_employed": round(total_self_employed, 2),
        "ympe": ympe,
        "yampe": yampe,
        "rate": cpp1_rate,
        "cpp2_rate": cpp2_rate,
        "basic_exemption": basic_exemption,
        "province": province,
        "year": year,
    }


def compute_cpp2_benefit(
    cpp2_average_earnings: float,
    start_age: int = 65,
    year: int = 2026,
    provider: Optional['TaxDataProvider'] = None,
) -> Dict:
    """Compute CPP2/QPP2 enhanced retirement benefit.

    Per DP#52: CPP2 contributions generate enhanced retirement benefits
    above the standard CPP benefit. The enhanced benefit uses the same
    1/40 accrual rate as the CPP enhancement (2019+).

    Formula:
    - CPP2 pensionable earnings = average earnings between YMPE and YAMPE
    - CPP2 annual benefit = (cpp2_average_earnings / YAMPE) × YAMPE × 1/40 × 12
    - Simplified: (cpp2_average_earnings / YAMPE) × max_cpp2_benefit

    The max CPP2 benefit at 65 = (YAMPE - YMPE) × 1/40 × 12
    For 2026: (81,900 - 74,600) × 0.025 × 12 = 2,190

    Early/late start adjustments follow the same rules as CPP1:
    - Before 65: 0.6% reduction per month (max 36% at 60)
    - After 65: 0.7% increase per month (max 42% at 70)

    Per DP#3: pure function. Same inputs → same outputs.
    Per DP#20: year-versioned via TaxDataProvider.

    References:
        CPP enhancement overview: https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-enhancement.html

    Args:
        cpp2_average_earnings: Average annual earnings in CPP2 band (between YMPE and YAMPE)
        start_age: Age to start CPP2 benefit (60-70)
        year: Taxation year for year-versioned data (DP#20)
        provider: Optional TaxDataProvider override

    Returns:
        Dict with cpp2_annual_benefit, cpp2_monthly_benefit, start_age,
        adjustment_factor, max_cpp2_benefit, cpp2_average_earnings
    """
    from tax_data import TaxDataProvider as _TDP

    if provider is None:
        provider = _TDP()

    data = provider.get_year_data(year, "canada", "federal")
    ympe = data.cpp_max_pensionable
    yampe = data.cpp2_max_pensionable

    # Max CPP2 benefit at age 65 = (YAMPE - YMPE) × 1/40 × 12
    # The 1/40 accrual rate is the same as CPP enhancement (2019+)
    cpp2_earnings_range = max(0, yampe - ympe)
    max_cpp2_benefit_65 = cpp2_earnings_range * CPP2_ACCRUAL_RATE * 12

    # Proportional benefit based on average earnings in CPP2 band
    if yampe > 0 and cpp2_average_earnings > 0:
        cpp2_base_benefit = (cpp2_average_earnings / yampe) * max_cpp2_benefit_65
    else:
        cpp2_base_benefit = 0.0

    # Apply early/late start adjustment (same as CPP1)
    start_age = max(CPP_EARLIEST_START_AGE, min(CPP_LATEST_START_AGE, start_age))
    if start_age < CPP_STANDARD_AGE:
        months_early = (CPP_STANDARD_AGE - start_age) * 12
        adjustment = 1 - months_early * CPP_EARLY_PENALTY_PER_MONTH
    elif start_age > CPP_STANDARD_AGE:
        months_late = (start_age - CPP_STANDARD_AGE) * 12
        adjustment = 1 + months_late * CPP_LATE_BONUS_PER_MONTH
    else:
        adjustment = 1.0

    cpp2_annual_benefit = cpp2_base_benefit * adjustment

    return {
        "cpp2_annual_benefit": round(cpp2_annual_benefit, 2),
        "cpp2_monthly_benefit": round(cpp2_annual_benefit / 12, 2),
        "start_age": start_age,
        "adjustment_factor": round(adjustment, 4),
        "max_cpp2_benefit_65": round(max_cpp2_benefit_65, 2),
        "cpp2_average_earnings": cpp2_average_earnings,
        "ympe": ympe,
        "yampe": yampe,
        "year": year,
    }


def compute_survivor_benefit(
    deceased_cpp_annual: float,
    survivor_age: int,
    survivor_own_cpp_annual: float = 0.0,
    province: str = "ontario",
    deceased_cpp2_annual: float = 0.0,
    year: int = 2026,
    provider: Optional['TaxDataProvider'] = None,
) -> Dict:
    """Compute CPP/QPP survivor benefit for a surviving spouse.

    Per DP#52: models both CPP and QPP survivor benefits.

    CPP survivor pension (Service Canada):
    - Age 65+: 60% of deceased's CPP retirement pension
    - Age 45-64: flat rate + 37.5% of deceased's CPP (not modeled in detail here)
    - Under 45 (not disabled): reduced flat rate
    - Combined survivor + own retirement pension cannot exceed individual max

    QPP survivor pension (Retraite Québec):
    - Flat-rate component + 37.5% of deceased's QPP
    - QPP has its own maximums and flat-rate amounts
    - Combined survivor + own pension cannot exceed individual QPP max

    CPP2/QPP2 enhanced benefits are included in the deceased's total
    pension for survivor benefit calculation.

    Per DP#3: pure function. Same inputs → same outputs.
    Per DP#20: year-versioned data via TaxDataProvider.

    References:
        CPP survivor: https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-survivor-benefit.html
        QPP survivor: https://www.retr.quebec.ca/en/actualites/régime-de-retraite-du-québec

    Args:
        deceased_cpp_annual: Deceased spouse's annual CPP1/QPP1 retirement benefit
        survivor_age: Surviving spouse's age
        survivor_own_cpp_annual: Surviving spouse's own CPP/QPP retirement benefit
        province: Province code (determines CPP vs QPP rules)
        deceased_cpp2_annual: Deceased spouse's CPP2/QPP2 enhanced benefit (DP#52)
        year: Taxation year (DP#20)
        provider: Optional TaxDataProvider override

    Returns:
        Dict with survivor_benefit, own_cpp, combined, max_benefit,
        province, pension_plan
    """
    from tax_data import TaxDataProvider as _TDP

    if provider is None:
        provider = _TDP()

    is_quebec = province.lower() in ("quebec", "qc")

    # Load year-versioned data (DP#20)
    try:
        data = provider.get_year_data(year, "canada", province.lower())
    except (ValueError, KeyError):
        data = provider.get_year_data(year, "canada", "federal")

    # Total deceased pension includes CPP2/QPP2
    deceased_total = deceased_cpp_annual + deceased_cpp2_annual

    if deceased_total <= 0:
        return {
            "survivor_benefit": 0.0,
            "own_cpp": survivor_own_cpp_annual,
            "combined": survivor_own_cpp_annual,
            "max_benefit": data.cpp_max_benefit_65,
            "province": province,
            "pension_plan": "QPP" if is_quebec else "CPP",
            "deceased_total": 0.0,
            "year": year,
        }

    if is_quebec:
        # QPP survivor benefit: flat rate + 37.5% of deceased's QPP
        qpp_survivor_flat = data.qpp_survivor_flat_rate
        survivor_benefit = qpp_survivor_flat + deceased_total * QPP_SURVIVOR_RATE

        # QPP max combined = own QPP + survivor cannot exceed individual max
        max_combined = data.qpp_max_benefit_65 if data.qpp_max_benefit_65 > 0 else data.cpp_max_benefit_65
    else:
        # CPP survivor benefit
        if survivor_age >= 65:
            survivor_benefit = deceased_total * CPP_SURVIVOR_RATE_65_PLUS
        else:
            # Under 65: flat rate (simplified) + 37.5% of deceased's CPP
            # Service Canada: flat portion + percentage portion
            # For simplicity, use 37.5% of deceased's pension for under 65
            # (the actual formula is more complex with disability status)
            survivor_benefit = deceased_total * CPP_SURVIVOR_RATE_UNDER_65

        # CPP max combined benefit
        max_combined = data.cpp_max_benefit_65

    # Combined survivor + own pension cannot exceed individual maximum
    combined = survivor_own_cpp_annual + survivor_benefit
    if combined > max_combined:
        # Reduce survivor benefit to cap at maximum
        survivor_benefit = max(0, max_combined - survivor_own_cpp_annual)
        combined = max_combined

    return {
        "survivor_benefit": round(survivor_benefit, 2),
        "own_cpp": survivor_own_cpp_annual,
        "combined": round(combined, 2),
        "max_benefit": max_combined,
        "province": province,
        "pension_plan": "QPP" if is_quebec else "CPP",
        "deceased_total": deceased_total,
        "year": year,
    }


# =============================================================================
# CPP/QPP Credit Splitting on Relationship Breakdown — ITA s.55.1  (issue #310)
# =============================================================================
#
# Credit splitting (a.k.a. Division of Unadjusted Pensionable Earnings, "DUPE")
# is a SEPARATE program from the voluntary pension-sharing modelled above. When
# a married or common-law couple SEPARATES or DIVORCES, the CPP pensionable
# earnings (credits) that EACH partner accumulated DURING the period they lived
# together are added together and split EQUALLY between them. This permanently
# re-writes each person's CPP earnings record and therefore affects their
# eventual retirement (and survivor/disability) pension — unlike pension
# sharing, it survives the relationship and does not require either party to be
# receiving a pension.
#
# Key rules (Service Canada / Retraite Québec):
#   - Triggered by separation or divorce (not by mutual choice while together).
#   - Only earnings during the months of cohabitation are pooled.
#   - The pooled earnings for each year of cohabitation are divided 50/50.
#   - For CPP, credit splitting is generally MANDATORY on divorce (either party,
#     or Service Canada, can apply; a province may opt out by agreement only
#     where provincial law permits). For QPP, partition is requested on
#     separation/divorce.
#   - It can HELP the lower-earning partner and REDUCE the higher earner's
#     credits; the combined credits during cohabitation are conserved.
#
# This is referenced in the ITA at s.55.1 (DUPE / division of pensionable
# earnings on breakdown), implemented in the CPP via s.55/s.55.1 of the
# Canada Pension Plan and the parallel QPP provisions.
#
# Source: CPP credit splitting after relationship breakdown:
#   https://www.canada.ca/en/services/benefits/publicpensions/cpp/credit-splitting-after-relationship-breakdown.html
#         ITA s.55.1 (DUPE on breakdown):
#   https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-55.1.html


@dataclass
class CreditSplitResult:
    """Result of a CPP/QPP credit-split on relationship breakdown (DUPE)."""
    eligible: bool = False
    cohabitation_years: int = 0
    primary_credits_before: float = 0.0
    spouse_credits_before: float = 0.0
    pooled_credits: float = 0.0
    primary_credits_after: float = 0.0
    spouse_credits_after: float = 0.0
    primary_credit_change: float = 0.0
    spouse_credit_change: float = 0.0
    combined_preserved: float = 0.0
    reasons: List[str] = field(default_factory=list)


def compute_credit_split(
    primary_cohab_earnings: float,
    spouse_cohab_earnings: float,
    cohabitation_start_year: int,
    cohabitation_end_year: int,
    relationship_ended: bool = True,
) -> CreditSplitResult:
    """Compute a CPP/QPP credit split (DUPE) on relationship breakdown.

    Per ITA s.55.1: on separation/divorce, the CPP pensionable earnings each
    partner accumulated during the period of cohabitation are pooled and split
    equally. Only the cohabitation-period earnings are affected; earnings before
    the relationship or after the breakdown stay with the person who earned them.

    Pure function (DP#3): same inputs → same output. Data-driven (DP#8): the
    pensionable-earnings totals are provided by the caller (DP#30 — we model
    the consequence of the split, we don't predict CPP records).

    Args:
        primary_cohab_earnings: Primary's CPP pensionable earnings DURING the
            cohabitation period (sum of unadjusted pensionable earnings).
        spouse_cohab_earnings: Spouse's CPP pensionable earnings during cohabitation.
        cohabitation_start_year: Year cohabitation began.
        cohabitation_end_year: Year of separation/divorce (end of cohabitation).
        relationship_ended: Whether the relationship has actually ended
            (credit splitting is only triggered by separation/divorce).

    Returns:
        CreditSplitResult with each partner's credits before/after the split.
    """
    reasons: List[str] = []

    if not relationship_ended:
        reasons.append(
            "Credit splitting requires separation or divorce (ITA s.55.1); "
            "not available while the relationship is intact."
        )
        return CreditSplitResult(
            eligible=False,
            primary_credits_before=primary_cohab_earnings,
            spouse_credits_before=spouse_cohab_earnings,
            primary_credits_after=primary_cohab_earnings,
            spouse_credits_after=spouse_cohab_earnings,
            combined_preserved=primary_cohab_earnings + spouse_cohab_earnings,
            reasons=reasons,
        )

    cohab_years = max(0, cohabitation_end_year - cohabitation_start_year)
    if cohab_years <= 0:
        reasons.append("No cohabitation period to split.")
        return CreditSplitResult(
            eligible=False,
            cohabitation_years=0,
            primary_credits_before=primary_cohab_earnings,
            spouse_credits_before=spouse_cohab_earnings,
            primary_credits_after=primary_cohab_earnings,
            spouse_credits_after=spouse_cohab_earnings,
            combined_preserved=primary_cohab_earnings + spouse_cohab_earnings,
            reasons=reasons,
        )

    pooled = primary_cohab_earnings + spouse_cohab_earnings
    # DUPE: the pooled cohabitation-period earnings are divided 50/50.
    each_after = pooled / 2.0

    reasons.append(
        f"Credit split over {cohab_years} cohabitation years: pooled CPP earnings "
        f"of ${pooled:,.0f} divided equally (ITA s.55.1 DUPE)."
    )

    return CreditSplitResult(
        eligible=True,
        cohabitation_years=cohab_years,
        primary_credits_before=primary_cohab_earnings,
        spouse_credits_before=spouse_cohab_earnings,
        pooled_credits=pooled,
        primary_credits_after=each_after,
        spouse_credits_after=each_after,
        primary_credit_change=each_after - primary_cohab_earnings,
        spouse_credit_change=each_after - spouse_cohab_earnings,
        combined_preserved=each_after * 2.0,
        reasons=reasons,
    )


def credit_split_pension_impact(
    result: CreditSplitResult,
    cohab_years_in_contributory_period: int,
    total_contributory_years: int,
    person: str = "spouse",
) -> Dict:
    """Estimate the retirement-pension impact of a credit split for one partner.

    The CPP retirement pension is, broadly, proportional to a person's average
    lifetime pensionable earnings. A credit split changes only the
    cohabitation-period earnings, so the approximate change in eventual pension
    base for a partner is the change in their pooled credits weighted by the
    share of their contributory period that the cohabitation represents.

    This is an APPROXIMATION (DP#30: we model the consequence, not the exact
    Service Canada record). The exact pension is computed by Service Canada
    using the full earnings history and the general drop-out provisions.

    Pure function (DP#3).

    Args:
        result: A CreditSplitResult from compute_credit_split.
        cohab_years_in_contributory_period: Cohabitation years that fall within
            the person's CPP contributory period.
        total_contributory_years: Length of the person's contributory period.
        person: 'primary' or 'spouse'.

    Returns:
        Dict with the per-partner credit change and an approximate share of the
        earnings base affected.
    """
    if not result.eligible or total_contributory_years <= 0:
        return {
            'person': person,
            'credit_change': 0.0,
            'earnings_base_share': 0.0,
            'note': 'No credit split applies.',
        }

    credit_change = (
        result.primary_credit_change if person == "primary"
        else result.spouse_credit_change
    )
    share = min(1.0, max(0.0,
        cohab_years_in_contributory_period / total_contributory_years))

    return {
        'person': person,
        'credit_change': credit_change,
        'earnings_base_share': share,
        # Positive => this person's average earnings base rises (higher pension);
        # negative => it falls. Weighted by how much of their contributory
        # period the split touches.
        'approx_base_change': credit_change * share,
        'note': (
            'Approximate: actual CPP pension is computed by Service Canada over '
            'the full earnings history with drop-out provisions.'
        ),
    }


# =============================================================================
# Year-by-Year CPP Sharing Projection
# =============================================================================

def project_cpp_sharing(
    data: CPPSharingInput,
    years: int = 25,
    inflation: float = 0.025,
    brackets: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Project CPP sharing over multiple years.

    Per DP#26: the projection is a fold over years, with each step
    being a pure function of (state, year) → (result, next_state).

    Args:
        data: Initial CPPSharingInput
        years: Number of years to project
        inflation: CPP benefit inflation rate (default 2.5%)
        brackets: Tax brackets for MTR calculation

    Returns:
        List of year-by-year results
    """
    from countries.canada.pension_split_optimizer import optimize_pension_split

    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets()

    results = []

    # Create mutable working copies
    primary_cpp = data.primary_cpp_annual
    spouse_cpp = data.spouse_cpp_annual
    primary_age = data.primary_age_in(data.calculation_year)
    spouse_age = data.spouse_age_in(data.calculation_year)

    for yr in range(years):
        sim_year = data.calculation_year + yr
        p_age = primary_age + yr
        s_age = spouse_age + yr

        # Only compute if both are 60+ and still cohabiting
        year_data = CPPSharingInput(
            primary_birth_year=data.primary_birth_year,
            spouse_birth_year=data.spouse_birth_year,
            primary_cpp_annual=primary_cpp,
            spouse_cpp_annual=spouse_cpp,
            primary_prb_annual=data.primary_prb_annual,
            spouse_prb_annual=data.spouse_prb_annual,
            primary_start_age=data.primary_start_age,
            spouse_start_age=data.spouse_start_age,
            cohabitation_start_year=data.cohabitation_start_year,
            cohabitation_end_year=data.cohabitation_end_year,
            primary_contribution_start_year=data.primary_contribution_start_year,
            spouse_contribution_start_year=data.spouse_contribution_start_year,
            province=data.province,
            calculation_year=sim_year,
            brackets=brackets,
        )

        if p_age >= 60 and s_age >= 60:
            sharing_benefit = optimize_cpp_sharing(year_data, brackets)
            annual_savings = sharing_benefit.get("annual_tax_savings", 0)
            eligible = sharing_benefit.get("recommendation", "not_eligible") != "not_eligible"
        else:
            sharing_benefit = None
            annual_savings = 0
            eligible = False

        results.append({
            "year": sim_year,
            "primary_age": p_age,
            "spouse_age": s_age,
            "primary_cpp_before": primary_cpp,
            "spouse_cpp_before": spouse_cpp,
            "cpp_sharing_eligible": eligible,
            "annual_tax_savings": annual_savings,
            "monthly_tax_savings": annual_savings / 12 if annual_savings else 0,
            "sharing_result": sharing_benefit,
        })

        # CPP benefits increase with inflation
        primary_cpp *= (1 + inflation)
        spouse_cpp *= (1 + inflation)

    return results