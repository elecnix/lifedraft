#!/usr/bin/env python3
"""
Quebec LIF (FRV — Fonds de revenu viager) Withdrawal Factors

Quebec LIFs (FRVs) use the QMFR (Quebec Maximum Funding Rate / Taux
maximal de financement) as the prescribed interest rate in the C/F
actuarial formula per PBSR s.20.1.

Key differences from federal LIF:
- Reference rate: QMFR (published by Retraite Québec) instead of CILR
- Quebec removed LIF maximum for ages 55+ effective 2025
- Quebec FRV specific minimum/maximum factors may apply for certain
  pension plan origins (SPP/Compluvie)
- Temporary income withdrawal (retrait temporaire) rules differ

Per DP#10: Quebec-specific factors in provinces/quebec/ directory,
mirroring political hierarchy.

Per DP#7: delegates to federal lif_maximum_withdrawal with jurisdiction='quebec'
to avoid duplicating the C/F formula logic.

References:
    Retraite Québec FRV: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/frv.aspx
    Quebec LIF maximum withdrawal: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx
    Quebec removal of LIF maximum for 55+: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx

Usage:
    from countries.canada.provinces.quebec.quebec_lif import (
        quebec_lif_maximum_withdrawal,
    )
"""

from typing import Dict

from countries.canada.locked_in_account import (
    lif_minimum_withdrawal,
    lif_maximum_withdrawal,
    LIF_MAX_WITHDRAWAL_FACTORS,
    QUEBEC_PRESCRIBED_RATES,
    get_ympe,
)


# =============================================================================
# Quebec LIF (FRV) temporary income — Retraite Québec (DP#20, issue #322)
# =============================================================================

# Temporary income (revenu temporaire) lets an FRV holder aged 54–64 draw an
# additional amount above the regular life income, capped at 40% of the year's
# maximum pensionable earnings (MPE/YMPE).
# Source: https://www.rrq.gouv.qc.ca/en/retraite/cri_frv/Pages/types_revenus_frv.aspx
QUEBEC_LIF_TEMPORARY_INCOME_MPE_FRACTION = 0.40
QUEBEC_LIF_TEMPORARY_INCOME_MIN_AGE = 54
QUEBEC_LIF_TEMPORARY_INCOME_MAX_AGE = 64  # eligible if under 65


# =============================================================================
# Quebec LIF maximum withdrawal factors
# =============================================================================

# Quebec-specific LIF maximum withdrawal factors (Retraite Québec)
# These override the federal OSFI factors for Quebec-governed LIFs.
# Key difference: Quebec removed the maximum for ages 55+ effective 2025.
#
# For ages under 55 in Quebec, the maximum withdrawal is calculated as
# prescribed_rate × balance (see QUEBEC_PRESCRIBED_RATES in locked_in_account.py).
# No factor table is needed for under-55 ages — they use the prescribed-rate formula.
#
# For ages 55+ before 2025, Quebec used the same C/F formula with QMFR as
# reference rate. The 2024 values below use OSFI 2024 factors as a
# conservative approximation since Quebec-specific QMFR Schedule 0.6
# factors for 2024 are not publicly available. The OSFI factors use the
# CANSIM rate (3.26% for 2024) which is lower than the QMFR rate (6.25%),
# so they underestimate the Quebec maximum. This is documented and acceptable
# as a conservative approximation for historical data.
#
# Official QMFR Schedule 0.6 factors are published by Retraite Québec at:
# https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx
# As of 2025+, Quebec removed the LIF maximum for ages 55+, making the
# QMFR Schedule 0.6 factors relevant only for 2024 and earlier years.
QUEBEC_LIF_MAX_WITHDRAWAL_FACTORS = {
    # 2024 Quebec factors not available. OSFI 2024 factors underestimate
    # Quebec maximums (CANSIM 3.26% < QMFR 6.25%). The nearest-year fallback
    # will use OSFI factors, which is documented as underestimating.
    # 2025+: No maximum for ages 55+ (can withdraw entire balance).
    # Under-55 ages use the prescribed-rate formula, not factor tables.
}


# =============================================================================
# Pure Functions — Quebec LIF Withdrawal
# =============================================================================

def quebec_lif_maximum_withdrawal(balance: float, age: int, year: int,
                                    temporary_income: float = 0) -> float:
    """Quebec LIF (FRV) maximum withdrawal.

    Delegates to the federal lif_maximum_withdrawal with jurisdiction='quebec',
    which handles the age 55+ no-maximum rule (effective 2025) and the
    prescribed-rate formula for under-55 ages.

    Rules by age and year:
    - Ages 55+ in 2025+: no maximum (returns full balance)
    - Ages under 55: prescribed_rate × balance - temporary_income (Article 20)
    - Ages 55+ before 2025: uses Quebec-specific C/F actuarial factors

    Args:
        balance: FRV balance at start of year
        age: Owner's age at start of year
        year: Calendar year (for year-versioned factors, DP#20)
        temporary_income: Quebec temporary income deduction for under-55
            FRV holders per Article 20 (default 0)

    Returns:
        Maximum withdrawal amount
    """
    return lif_maximum_withdrawal(balance, age, year,
                                   factors=QUEBEC_LIF_MAX_WITHDRAWAL_FACTORS,
                                   jurisdiction='quebec',
                                   temporary_income=temporary_income)


def quebec_lif_withdrawal_range(balance: float, age: int, year: int,
                                    temporary_income: float = 0) -> Dict:
    """Compute Quebec LIF withdrawal range.

    Args:
        balance: FRV balance at start of year
        age: Owner's age at start of year
        year: Calendar year
        temporary_income: Quebec under-55 temporary income deduction (default 0)

    Returns:
        Dict with minimum, maximum, balance, and jurisdiction
    """
    minimum = lif_minimum_withdrawal(balance, age)
    maximum = quebec_lif_maximum_withdrawal(balance, age, year,
                                              temporary_income=temporary_income)
    # Guard: temporary_income can reduce maximum below minimum for Quebec
    # under-55. When minimum > maximum, no forced withdrawal applies;
    # clamp minimum to maximum so the range is valid.
    if minimum > maximum:
        minimum = maximum
    return {
        'minimum': minimum,
        'maximum': maximum,
        'balance': balance,
        'jurisdiction': 'quebec',
    }


def quebec_lif_temporary_income_max(age: int, year: int,
                                    other_temporary_income: float = 0.0) -> float:
    """Maximum Quebec FRV temporary income (revenu temporaire) for the year.

    An FRV holder aged 54 to under 65 (i.e. 54–64 on December 31 of the year
    preceding the application) may draw a temporary income capped at 40% of
    the year's maximum pensionable earnings (MPE/YMPE). Any temporary income
    already drawn from other sources reduces this ceiling.

    Per Retraite Québec, the application is valid only for the year filed and
    is independent of the life-income maximum. Holders outside the 54–64 age
    band are not eligible (returns 0).

    Pure function (DP#3): same inputs → same output.

    Args:
        age: Owner's age (in the application year)
        year: Calendar year (for year-versioned YMPE, DP#20)
        other_temporary_income: Temporary income drawn from other locked-in
            sources this year (reduces the available ceiling)

    Returns:
        Maximum temporary income available this year (0 if not eligible)

    Source: https://www.rrq.gouv.qc.ca/en/retraite/cri_frv/Pages/types_revenus_frv.aspx
    """
    if age < QUEBEC_LIF_TEMPORARY_INCOME_MIN_AGE or age > QUEBEC_LIF_TEMPORARY_INCOME_MAX_AGE:
        return 0.0
    ympe = get_ympe(year)
    ceiling = QUEBEC_LIF_TEMPORARY_INCOME_MPE_FRACTION * ympe
    return max(0.0, ceiling - max(0.0, other_temporary_income))


def quebec_lif_annuity_conversion(balance: float, annuity_rate: float,
                                  payments_per_year: int = 12) -> Dict:
    """Convert a Quebec FRV balance to a life annuity (rente viagère).

    An FRV holder may use all or part of the fund to purchase a life annuity
    that respects the locking-in rules (the annuity must be payable for life
    and is non-commutable). This computes the level periodic payment from the
    annuitized balance using a simple level-annuity factor.

    The periodic payment is balance / ä where ä is the annuity-immediate-style
    factor implied by ``annuity_rate`` and the number of payments. For a
    perpetual/level model we use payment = balance × annuity_rate / payments
    when a per-payment rate is supplied; callers pass an annual ``annuity_rate``
    representing the insurer's payout rate on the converted capital.

    This is the standard level-payout case; mortality-credit pricing and
    indexed annuities are out of scope (documented assumption).

    Pure function (DP#3): same inputs → same output.

    Args:
        balance: FRV balance being annuitized
        annuity_rate: Annual payout rate applied to the converted capital
        payments_per_year: Number of annuity payments per year (default 12)

    Returns:
        Dict with annuitized_balance, annual_payment, periodic_payment,
        and locked_in (always True — annuity preserves locking-in)

    Source: https://www.retraitequebec.gouv.qc.ca/en/professionnels/cri_frv/Pages/caracteristiques-frv.aspx
    """
    if balance <= 0:
        raise ValueError(f"balance must be positive, got {balance}")
    if annuity_rate < 0:
        raise ValueError(f"annuity_rate must be non-negative, got {annuity_rate}")
    if payments_per_year <= 0:
        raise ValueError(f"payments_per_year must be positive, got {payments_per_year}")
    annual_payment = balance * annuity_rate
    periodic_payment = annual_payment / payments_per_year
    return {
        'annuitized_balance': balance,
        'annual_payment': annual_payment,
        'periodic_payment': periodic_payment,
        'payments_per_year': payments_per_year,
        'locked_in': True,
    }
