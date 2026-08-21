#!/usr/bin/env python3
"""
Attribution Rules — ITA s.74.1, s.74.2, TOSI s.104.2

This module models Canadian attribution and TOSI (Tax On Split Income)
rules. These are different programs from spousal RRSP attribution
(which is in family.py).

Key attribution rules:
1. Spousal RRSP attribution (ITA s.146(8.3)) — in family.py
2. Spousal property transfer attribution (ITA s.74.1) — this module
3. Minor child attribution (ITA s.74.2) — this module
4. TOSI (ITA s.104.2) — this module
5. Prescribed-rate loan escape — in debt.py

Per DP#10: this module owns ITA s.74.1, s.74.2, and s.104.2.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Attribution entries
    ITA s.74.1 (spousal property attribution)
    ITA s.74.2 (minor child attribution)
    ITA s.104.2 (TOSI)
    Archived IT-511R: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/it511r/archived-interspousal-certain-other-transfers-loans-property.html

Usage:
    from countries.canada.attribution import check_attribution, check_tosi, AttributionResult

    result = check_attribution(
        transfer_type='property',
        donor_role='primary',
        recipient_role='spouse',
        years_since_transfer=2,
    )
    print(f"Attribution applies: {result.attributed}")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


# =============================================================================
# Enums
# =============================================================================

class TransferType(Enum):
    SPOUSAL_RRSP = "spousal_rrsp"      # Handled in family.py
    PROPERTY_TRANSFER = "property"      # ITA s.74.1: loan/transfer of property
    MINOR_CHILD = "minor_child"         # ITA s.74.2: transfer to child under 18
    TRUST = "trust"                     # Attribution through trust


class RecipientRole(Enum):
    SPOUSE = "spouse"
    COMMON_LAW = "common_law"
    MINOR_CHILD = "minor_child"
    ADULT_CHILD = "adult_child"
    OTHER = "other"


class IncomeType(Enum):
    """Type of attributed income — determines what gets attributed back."""
    INTEREST = "interest"
    DIVIDEND = "dividend"
    CAPITAL_GAIN = "capital_gain"
    BUSINESS_INCOME = "business_income"
    RENTAL_INCOME = "rental_income"
    ALL = "all"  # Both income and capital gains


class TOSIExclusion(Enum):
    """Exclusions from TOSI (reasons TOSI doesn't apply)."""
    EXCLUDED_SHARE = "excluded_share"          # 10%+ votes and value, 20+ hrs/week
    REASONABLE_RETURN = "reasonable_return"    # Amount is reasonable for work
    SPOUSE_OVER_25 = "spouse_over_25"         # Spouse age 25+ (limited)
    SOURCE_EXCLUDED = "source_excluded"        # Source type excluded by rules
    AGE_25_PLUS = "age_25_plus"               # Recipient age 25+ (for some rules)


# =============================================================================
# Attribution Result
# =============================================================================

@dataclass
class AttributionResult:
    """Result of an attribution check."""
    attributed: bool = False
    attribution_type: str = ""
    donor_role: str = ""
    recipient_role: str = ""
    income_types_attributed: List[IncomeType] = field(default_factory=list)
    years_until_clear: int = 0
    reason: str = ""
    escape_available: bool = False
    escape_description: str = ""


# =============================================================================
# Pure Functions — Attribution Checks
# =============================================================================

def check_attribution(
    transfer_type: TransferType,
    donor_role: str = "primary",
    recipient_role: str = "spouse",
    recipient_age: int = 0,
    years_since_transfer: int = 0,
    interest_paid_by_jan30: bool = True,
    prescribed_rate_used: bool = False,
    joint_election: bool = False,
) -> AttributionResult:
    """Check if attribution rules apply to a transfer.

    Pure function (DP#3): same inputs → same output.

    Attribution rules by transfer type:

    ITA s.74.1 — Spousal property transfer/loan:
    - Income AND capital gains attribute back to the transferor
    - Applies indefinitely (unless prescribed-rate loan exception)
    - Exception: loan at prescribed rate with interest paid by Jan 30

    ITA s.74.2 — Minor child transfer:
    - Only INCOME (not capital gains) attributes back to transferor
    - Applies while child is under 18
    - Capital gains stay in child's hands (important for planning)

    ITA s.146(8.3) — Spousal RRSP:
    - Handled in family.py (3 calendar year rule)

    Args:
        transfer_type: Type of transfer
        donor_role: Role of the person who transferred/loaned
        recipient_role: Role of the person who received
        recipient_age: Age of the recipient (at time of check)
        years_since_transfer: Years since the transfer/loan
        interest_paid_by_jan30: For prescribed-rate loans: interest paid by Jan 30?
        prescribed_rate_used: Was the prescribed rate used for the loan?
        joint_election: ITA s.74.1(2) — spouses jointly elect to have transferor include income, avoiding attribution

    Returns:
        AttributionResult with attribution details
    """
    if transfer_type == TransferType.PROPERTY_TRANSFER:
        return _check_spousal_property_attribution(
            donor_role, recipient_role, recipient_age,
            years_since_transfer, interest_paid_by_jan30, prescribed_rate_used,
            joint_election
        )
    elif transfer_type == TransferType.MINOR_CHILD:
        return _check_minor_child_attribution(
            donor_role, recipient_role, recipient_age,
        )
    elif transfer_type == TransferType.SPOUSAL_RRSP:
        # Delegated to family.py
        return AttributionResult(
            attributed=False,
            reason="Spousal RRSP attribution handled by family.py module",
        )
    else:
        return AttributionResult(
            attributed=False,
            reason=f"Unknown transfer type: {transfer_type.value}",
        )


def _check_spousal_property_attribution(
    donor_role: str, recipient_role: str, recipient_age: int,
    years_since_transfer: int, interest_paid_by_jan30: bool,
    prescribed_rate_used: bool, joint_election: bool,
) -> AttributionResult:
    """ITA s.74.1: Spousal property transfer/loan attribution.

    Key rules:
    - Both income AND capital gains attribute back (indefinitely)
    - Exception: loan at prescribed rate with interest paid by Jan 30
    - Exception: joint election under ITA s.74.1(2) — spouses can jointly
      elect to have the transferor include the income, avoiding attribution.
      This is useful when the transferor has a lower marginal rate.
    - Exception does NOT apply if interest not paid by Jan 30
    """
    # Joint election under ITA s.74.1(2)
    if joint_election:
        return AttributionResult(
            attributed=False,
            attribution_type="spousal_property",
            donor_role=donor_role,
            recipient_role=recipient_role,
            income_types_attributed=[],
            years_until_clear=0,
            reason="Joint election (ITA s.74.1(2)): spouses elected to have transferor include income, attribution does NOT apply",
            escape_available=True,
            escape_description="Joint election (ITA s.74.1(2)): transferor includes income instead of attribution",
        )

    # Prescribed-rate loan exception
    if prescribed_rate_used and interest_paid_by_jan30:
        return AttributionResult(
            attributed=False,
            attribution_type="spousal_property",
            donor_role=donor_role,
            recipient_role=recipient_role,
            income_types_attributed=[],
            years_until_clear=0,
            reason="Prescribed-rate loan with interest paid by Jan 30: attribution does NOT apply",
            escape_available=True,
            escape_description="Prescribed-rate loan exception (ITA s.74.1(2))",
        )

    # Attribution applies (indefinitely)
    return AttributionResult(
        attributed=True,
        attribution_type="spousal_property",
        donor_role=donor_role,
        recipient_role=recipient_role,
        income_types_attributed=[IncomeType.ALL],
        years_until_clear=0,  # No time limit for s.74.1
        reason="Property transfer/loan to spouse: income AND capital gains attribute back indefinitely (ITA s.74.1)",
        escape_available=prescribed_rate_used,
        escape_description="Pay interest by Jan 30 each year to avoid attribution",
    )


def _check_minor_child_attribution(
    donor_role: str, recipient_role: str, recipient_age: int,
) -> AttributionResult:
    """ITA s.74.2: Minor child attribution.

    Key rules:
    - Only INCOME attributes back (NOT capital gains)
    - Applies while child is under 18
    - Capital gains are NOT attributed — this is the key planning point
    """
    is_minor = recipient_age < 18

    if not is_minor:
        return AttributionResult(
            attributed=False,
            attribution_type="minor_child",
            donor_role=donor_role,
            recipient_role=recipient_role,
            income_types_attributed=[],
            years_until_clear=0,
            reason=f"Child is {recipient_age}: not a minor, attribution does NOT apply",
        )

    return AttributionResult(
        attributed=True,
        attribution_type="minor_child",
        donor_role=donor_role,
        recipient_role=recipient_role,
        income_types_attributed=[IncomeType.INTEREST, IncomeType.DIVIDEND, IncomeType.BUSINESS_INCOME],
        years_until_clear=18 - recipient_age,
        reason=f"Child is {recipient_age}: INCOME (not capital gains) attributes back to {donor_role} (ITA s.74.2). Capital gains stay in child's hands.",
        escape_available=False,
        escape_description="No escape for minor child attribution (short of prescribed-rate loan)",
    )


# =============================================================================
# TOSI (Tax On Split Income) — ITA s.104.2
# =============================================================================

def check_tosi(
    recipient_age: int,
    source_type: str = "business",
    recipient_hours_per_week: float = 0,
    recipient_ownership_pct: float = 0,
    source_is_excluded_share: bool = False,
    is_spouse: bool = False,
    income_amount: float = 0,
    reasonable_return_amount: float = 0,
    top_marginal_rate: float = 0.5325,
) -> Dict:
    """Check if TOSI applies to split income.

    TOSI (Tax On Split Income) taxes certain income at the top
    marginal rate when it's received by a related person who didn't
    meaningfully contribute to earning it.

    Exclusions from TOSI:
    1. Excluded shares: recipient owns 10%+ of votes and value,
       works 20+ hours/week in the business
    2. Reasonable return: amount is reasonable for work performed
    3. Spouse age 25+: some types of split income are excluded
       if the spouse is 25 or older
    4. Age 25+ for certain sources: recipient age 25+ excludes
       some income types

    Pure function (DP#3).

    Args:
        recipient_age: Age of the person receiving the income
        source_type: Type of source ('business', 'property', 'dividend', 'trust')
        recipient_hours_per_week: Hours/week the recipient works in the business
        recipient_ownership_pct: Ownership percentage (votes + value)
        source_is_excluded_share: Whether the share is officially excluded
        is_spouse: Whether the recipient is the spouse
        income_amount: Amount of split income received
        reasonable_return_amount: What would be a reasonable return for work done

    Returns:
        Dict with TOSI applicability and details
    """
    # Check exclusions
    exclusions = []
    tosi_applies = True
    # DP#12/DP#27: top_marginal_rate is a parameter, not hardcoded.
    # Default 0.5325 = combined federal (33%) + Quebec (25%) top marginal rate
    # for Quebec-resident households. Callers should pass province-specific rate
    # from tax_calculator for accuracy.

    # Exclusion 1: Excluded share (10%+ votes+value, 20+ hrs/week)
    if source_is_excluded_share:
        exclusions.append(TOSIExclusion.SOURCE_EXCLUDED)
        tosi_applies = False

    if recipient_ownership_pct >= 0.10 and recipient_hours_per_week >= 20:
        exclusions.append(TOSIExclusion.EXCLUDED_SHARE)
        tosi_applies = False

    # Exclusion 2: Reasonable return
    if reasonable_return_amount > 0 and income_amount <= reasonable_return_amount:
        exclusions.append(TOSIExclusion.REASONABLE_RETURN)
        tosi_applies = False

    # Exclusion 3: Age 25+
    if recipient_age >= 25 and source_type in ('business', 'dividend'):
        exclusions.append(TOSIExclusion.AGE_25_PLUS)
        tosi_applies = False

    # Exclusion 4: Spouse over 25 (limited)
    if is_spouse and recipient_age >= 25:
        exclusions.append(TOSIExclusion.SPOUSE_OVER_25)
        # Note: spouse exclusion has limitations — not all income types covered

    # Compute TOSI tax if applicable
    tosi_tax = 0
    if tosi_applies and income_amount > 0:
        tosi_tax = income_amount * top_marginal_rate

    return {
        'tosi_applies': tosi_applies,
        'recipient_age': recipient_age,
        'source_type': source_type,
        'exclusions': [e.value for e in exclusions],
        'income_amount': income_amount,
        'tosi_tax': tosi_tax,
        'top_marginal_rate': top_marginal_rate,
        'reason': _tosi_reason(tosi_applies, exclusions, recipient_age, source_type),
    }


def _tosi_reason(tosi_applies: bool, exclusions: List[TOSIExclusion],
                  age: int, source_type: str) -> str:
    """Generate human-readable TOSI reason."""
    if not tosi_applies:
        if TOSIExclusion.EXCLUDED_SHARE in exclusions:
            return "Excluded share: 10%+ ownership and 20+ hours/week — TOSI does not apply"
        if TOSIExclusion.AGE_25_PLUS in exclusions:
            return f"Age {age}: 25+ exclusion for {source_type} income — TOSI does not apply"
        if TOSIExclusion.REASONABLE_RETURN in exclusions:
            return "Income is reasonable return for work performed — TOSI does not apply"
        if TOSIExclusion.SOURCE_EXCLUDED in exclusions:
            return "Source is an excluded share — TOSI does not apply"
        return "TOSI does not apply (exclusion found)"

    return f"TOSI applies: {source_type} income received at age {age} taxed at top marginal rate"


# =============================================================================
# Attribution Planning Summary
# =============================================================================

def attribution_planning_summary(
    spouse_age: int = 46,
    child_ages: List[int] = None,
    has_prescribed_rate_loan: bool = False,
    interest_paid_on_time: bool = True,
) -> Dict:
    """Generate a comprehensive attribution planning summary.

    This is a convenience function that checks all relevant attribution
    rules for a given family situation.
    """
    child_ages = child_ages or []

    # Spousal property attribution
    spousal_result = check_attribution(
        TransferType.PROPERTY_TRANSFER,
        donor_role="primary",
        recipient_role="spouse",
        recipient_age=spouse_age,
        interest_paid_by_jan30=interest_paid_on_time,
        prescribed_rate_used=has_prescribed_rate_loan,
    )

    # Minor child attribution
    child_results = []
    for age in child_ages:
        child_result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=age,
        )
        child_results.append({'age': age, 'result': child_result})

    return {
        'spousal_attribution': {
            'attributed': spousal_result.attributed,
            'reason': spousal_result.reason,
            'escape': spousal_result.escape_description,
        },
        'minor_child_attribution': [
            {
                'age': c['age'],
                'attributed': c['result'].attributed,
                'types_attributed': [t.value for t in c['result'].income_types_attributed],
                'reason': c['result'].reason,
            }
            for c in child_results
        ],
        'planning_notes': _generate_planning_notes(spousal_result, child_results),
    }


def _generate_planning_notes(spousal_result, child_results) -> List[str]:
    """Generate actionable planning notes from attribution analysis."""
    notes = []

    if spousal_result.attributed:
        if spousal_result.escape_available:
            notes.append(
                "💡 SPOUSAL: Use a prescribed-rate loan at 2% to escape attribution. "
                "Pay interest by Jan 30 each year."
            )
        else:
            notes.append(
                "⚠️ SPOUSAL: Property transfer to spouse triggers attribution of "
                "income AND capital gains back to transferor. Consider prescribed-rate loan."
            )

    for c in child_results:
        if c['result'].attributed:
            age = c['age']
            notes.append(
                f"💡 CHILD (age {age}): Income attributes back to parent, "
                f"but CAPITAL GAINS do NOT. Consider gifting assets with growth potential "
                f"to the child's account. Attribution clears at age 18."
            )

    if not spousal_result.attributed and all(not c['result'].attributed for c in child_results):
        notes.append("✅ No attribution issues — all transfers/loans are structured properly.")

    return notes


# =============================================================================
# Below-Market Interest Loan Attribution — ITA s.74.5(1)  (issue #311)
# =============================================================================
#
# ITA s.74.5(1) sets out the conditions under which a LOAN (as opposed to an
# outright transfer) escapes the spousal/related-minor attribution rules of
# s.74.1/s.74.2. The exception in s.74.5(2) only applies where BOTH of these
# are met:
#   (a) interest on the loan is charged at a rate that is at least the LESSER
#       of the CRA prescribed rate (s.74.5(2)(a)(i)) in effect when the loan
#       was made and the commercial rate that would have applied between
#       arm's-length parties (s.74.5(2)(a)(ii)); and
#   (b) that interest is actually PAID no later than 30 days after the end of
#       each year (i.e. by January 30 of the following year) — s.74.5(2)(b).
#
# When a loan is made at BELOW that rate (or carries no interest), the
# s.74.5(2) exception is unavailable and the property income earned on the
# loaned funds is attributed back to the lender under s.74.1(1)/s.74.2.
# Effectively, the lender is taxed on the income as if it remained theirs;
# the "interest savings" the borrower enjoyed do not escape the lender's
# return. (This is distinct from the prescribed-rate-loan planning already
# modelled in debt.py, which assumes the exception IS satisfied.)
#
# Note: s.74.5(2) freezes the comparison rate at LOAN INCEPTION. A later rise
# in the prescribed rate does not retroactively taint a loan that met the rate
# test when it was made, provided interest keeps being paid on time.
#
# Source: ITA s.74.5(1)/(2): https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-74.5.html
# CRA prescribed rates: https://www.canada.ca/en/revenue-agency/services/tax/prescribed-interest-rates.html


@dataclass
class BelowMarketLoanResult:
    """Result of a below-market interest loan attribution check (ITA s.74.5(1))."""
    attributed: bool = False
    loan_rate: float = 0.0
    required_rate: float = 0.0
    prescribed_rate: float = 0.0
    commercial_rate: float = 0.0
    interest_paid_by_jan30: bool = True
    attributed_income: float = 0.0
    reason: str = ""
    escape_description: str = ""


def check_below_market_loan_attribution(
    loan_principal: float,
    loan_rate: float,
    prescribed_rate: float,
    commercial_rate: Optional[float] = None,
    interest_paid_by_jan30: bool = True,
    income_earned: float = 0.0,
    recipient_role: str = "spouse",
    donor_role: str = "primary",
) -> BelowMarketLoanResult:
    """Check attribution on a below-market-interest loan to a related person.

    Implements ITA s.74.5(1)/(2): the loan escapes attribution ONLY if it
    carries interest at no less than the LESSER of (i) the CRA prescribed rate
    when the loan was made and (ii) the commercial rate, AND that interest is
    paid within 30 days of year-end (by Jan 30). A loan below that required
    rate — or one where the interest is not paid on time — fails the exception,
    so the property income earned on the loaned funds is attributed back to the
    lender under s.74.1/s.74.2.

    Pure function (DP#3): same inputs → same output. Data-driven (DP#8) — the
    prescribed and commercial rates are injected, never hardcoded (DP#12).

    Args:
        loan_principal: Loan amount (used only for context/reporting).
        loan_rate: Interest rate actually charged on the loan (e.g. 0.01 = 1%).
        prescribed_rate: CRA prescribed rate in effect when the loan was made.
        commercial_rate: Arm's-length commercial rate at loan inception. If
            None, the prescribed rate alone is used as the benchmark.
        interest_paid_by_jan30: Whether the year's interest was paid by Jan 30
            of the following year (s.74.5(2)(b)).
        income_earned: Property income earned on the loaned funds this year
            (the amount that would be attributed back if the exception fails).
        recipient_role: Role of the borrower (spouse / minor_child).
        donor_role: Role of the lender.

    Returns:
        BelowMarketLoanResult describing whether attribution applies.
    """
    # s.74.5(2)(a): benchmark is the LESSER of prescribed and commercial rate.
    if commercial_rate is not None:
        required_rate = min(prescribed_rate, commercial_rate)
    else:
        required_rate = prescribed_rate

    rate_test_met = loan_rate >= required_rate
    timing_test_met = interest_paid_by_jan30
    exception_available = rate_test_met and timing_test_met

    if exception_available:
        return BelowMarketLoanResult(
            attributed=False,
            loan_rate=loan_rate,
            required_rate=required_rate,
            prescribed_rate=prescribed_rate,
            commercial_rate=commercial_rate or 0.0,
            interest_paid_by_jan30=interest_paid_by_jan30,
            attributed_income=0.0,
            reason=(
                f"Loan rate {loan_rate*100:.2f}% meets the required "
                f"{required_rate*100:.2f}% (lesser of prescribed/commercial) and "
                "interest was paid by Jan 30: s.74.5(2) exception applies, NO attribution."
            ),
            escape_description="ITA s.74.5(2): loan meets rate + timely-interest tests",
        )

    if not rate_test_met:
        why = (
            f"Loan rate {loan_rate*100:.2f}% is BELOW the required "
            f"{required_rate*100:.2f}% (lesser of prescribed {prescribed_rate*100:.2f}% "
            f"and commercial rate)"
        )
    else:
        why = "interest was NOT paid by Jan 30"

    return BelowMarketLoanResult(
        attributed=True,
        loan_rate=loan_rate,
        required_rate=required_rate,
        prescribed_rate=prescribed_rate,
        commercial_rate=commercial_rate or 0.0,
        interest_paid_by_jan30=interest_paid_by_jan30,
        attributed_income=max(0.0, income_earned),
        reason=(
            f"Below-market loan to {recipient_role}: {why}. The s.74.5(2) exception "
            f"is unavailable, so property income (${max(0.0, income_earned):,.0f}) is "
            f"attributed back to {donor_role} under ITA s.74.1/s.74.2 (s.74.5(1))."
        ),
        escape_description=(
            "Charge at least the lesser of the prescribed/commercial rate AND "
            "pay the interest by Jan 30 each year (ITA s.74.5(2))"
        ),
    )


# =============================================================================
# Court-Ordered / Separation-Agreement Transfer Exception — ITA s.74.5(3)
# (issue #312)
# =============================================================================
#
# ITA s.74.5(3) turns off the s.74.1/s.74.2 attribution rules for property
# transferred to a spouse/common-law partner where, at the time of the
# transfer, the parties were living separate and apart by reason of a
# breakdown of their relationship. Specifically:
#   - s.74.5(3)(a): income/loss attribution under s.74.1 does NOT apply while
#     the parties are living separate and apart due to the breakdown; and
#   - s.74.5(3)(b): capital-gain attribution under s.74.2 does NOT apply to a
#     post-separation disposition IF both spouses jointly elect (in writing,
#     filed with the return) for s.74.2 not to apply.
#
# This is the statutory basis for the "separation-agreement exception" that
# also relieves court-ordered/decree transfers between (former) spouses on
# relationship breakdown. The same s.74.5(3) relief is what spares a spousal
# RRSP roll-over made under a written separation agreement or court order from
# the s.146(8.3) attribution rule.
#
# Source: ITA s.74.5(3): https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-74.5.html


def check_separation_transfer_exception(
    living_separate_and_apart: bool,
    due_to_breakdown: bool,
    is_capital_gain: bool = False,
    joint_election_74_2: bool = False,
) -> AttributionResult:
    """Check the ITA s.74.5(3) separation/breakdown attribution exception.

    Under s.74.5(3), spousal attribution is switched off for transfers made
    while the spouses are living separate and apart because of a relationship
    breakdown:
      - INCOME attribution (s.74.1) is automatically off — s.74.5(3)(a).
      - CAPITAL-GAIN attribution (s.74.2) is off only if both parties jointly
        elect for s.74.2 not to apply — s.74.5(3)(b).

    Pure function (DP#3).

    Args:
        living_separate_and_apart: Were the spouses living separate and apart
            at the relevant time?
        due_to_breakdown: Was the separation by reason of relationship breakdown?
        is_capital_gain: Is the income in question a capital gain (s.74.2)
            rather than ordinary income/loss (s.74.1)?
        joint_election_74_2: Did both spouses jointly elect for s.74.2 not to
            apply (required for the capital-gain exception)?

    Returns:
        AttributionResult — attributed=False when the s.74.5(3) exception frees
        the transfer from attribution.
    """
    qualifies = living_separate_and_apart and due_to_breakdown

    if not qualifies:
        # The s.74.5(3) exception does not apply, so normal s.74.1/s.74.2
        # attribution stands: `attributed=True` regardless of income type.
        # DP#13/#32 (#726): `is_capital_gain` must actually branch here -- a
        # prior version wrote `attributed=is_capital_gain or True`, which is
        # always True and left the flag dead, so a capital gain was never
        # marked CAPITAL_GAIN and was indistinguishable from ordinary income
        # (the capital-gains attribution path was unreachable). The flag now
        # drives `income_types_attributed`: s.74.2 for a capital gain,
        # s.74.1 (all income) otherwise -- matching `_check_spousal_property_attribution`.
        income_types = ([IncomeType.CAPITAL_GAIN] if is_capital_gain
                        else [IncomeType.ALL])
        return AttributionResult(
            attributed=True,
            attribution_type="separation_exception",
            donor_role="primary",
            recipient_role="spouse",
            income_types_attributed=income_types,
            reason=(
                "Not living separate and apart due to breakdown: s.74.5(3) "
                "exception does not apply; normal s.74.1/s.74.2 attribution stands."
            ),
            escape_available=False,
        )

    if is_capital_gain and not joint_election_74_2:
        return AttributionResult(
            attributed=True,
            attribution_type="separation_exception",
            donor_role="primary",
            recipient_role="spouse",
            income_types_attributed=[IncomeType.CAPITAL_GAIN],
            reason=(
                "Living separate and apart due to breakdown, but capital-gain "
                "attribution (s.74.2) is relieved only with a joint election "
                "under s.74.5(3)(b). No election made: s.74.2 still applies."
            ),
            escape_available=True,
            escape_description="File a joint election for s.74.2 not to apply (s.74.5(3)(b))",
        )

    return AttributionResult(
        attributed=False,
        attribution_type="separation_exception",
        donor_role="primary",
        recipient_role="spouse",
        income_types_attributed=[],
        reason=(
            "Living separate and apart due to relationship breakdown: ITA "
            "s.74.5(3) switches off spousal attribution"
            + (" (capital gains relieved by joint election under s.74.5(3)(b))"
               if is_capital_gain else " on income/loss (s.74.5(3)(a))")
            + "."
        ),
        escape_available=True,
        escape_description="ITA s.74.5(3): separation/breakdown attribution exception",
    )