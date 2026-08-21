#!/usr/bin/env python3
"""
Locked-In Retirement Account (CRI/LIRA) and LIF Conversion Module

CRI (Compte de retraite immobilisé, Quebec) / LIRA (Locked-In Retirement
Account, federal/other provinces) hold pension funds transferred from
employer pension plans. Funds cannot be withdrawn except in specific
permitted circumstances. Must eventually convert to a LIF (Life Income
Fund / Fonds de revenu viager) by December 31 of the year the owner
turns 71.

Per DP#10: this module owns CRI/LIRA accumulation, conversion gate
(DP#28), and LIF withdrawal rules.

Per DP#16: auto-include when `lira_balance` or `cri_balance` appears
in input data.

Key rules:
- CRI/LIRA: no withdrawals except hardship, small-balance unlock,
  non-resident, shortened life expectancy, or death
- Unlocking grounds are jurisdiction-specific: federal (PBSA/PBSR) and
  Quebec recognize non-residency and shortened life expectancy only;
  Ontario additionally allows financial-hardship unlocking
- Small-balance/shortened-life grounds may be taken as a tax-deferred
  transfer to an RRSP/RRIF (LIRA→RRSP transfer) rather than a cash lump sum
- On death the balance ceases to be locked in: a surviving spouse may roll it
  over tax-deferred to their RRSP/RRIF (Quebec spouse priority over the will);
  otherwise it is a taxable lump sum to the estate
- Mandatory conversion to LIF by Dec 31 of year owner turns 71
- LIF minimum withdrawal: same as RRIF minimums
- LIF maximum withdrawal: C/F actuarial formula per PBSR s.20.1
- Small-balance unlock: balance < 50% YMPE (federal) or 40% YMPE (Ontario)
  and age >= 55 → lump-sum permitted
- CRI/LIRA balance does NOT consume RRSP contribution room
- LIF withdrawals are taxable as regular income
- LIF withdrawals can trigger OAS clawback

References:
    Retraite Québec CRI: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/cri.aspx
    Retraite Québec FRV/LIF: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/frv.aspx
    CRA LIF pamphlet: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/rc4167/life-income-fund.html
    ITA subsection 146(1) definition of "locked-in RRSP"
    OSFI LIF maximum factors: https://www.osfi-bsif.gc.ca/en/supervision/regulated-entities/pension-plans/lif-maximum-withdrawal
    PBSR s.20.1: Pension Benefits Standards Regulations

Usage:
    from countries.canada.locked_in_account import (
        LockedInAccount, LIFFund, lif_minimum_withdrawal,
        lif_maximum_withdrawal, must_convert_by_year,
    )

    account = LockedInAccount(balance=100000, birth_year=1960)
    if account.must_convert(2031):
        lif = account.convert_to_lif(year=2031)
        min_w = lif.minimum_withdrawal(2031)
        max_w = lif.maximum_withdrawal(2031)
"""

from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from countries.canada.retirement import _get_rrif_rates
from countries.canada.account_models import _apply_growth


# =============================================================================
# YMPE — Year-versioned data (DP#12, DP#20)
# =============================================================================

YMPE_BY_YEAR = {
    2020: 58700,
    2021: 61600,
    2022: 64900,
    2023: 66600,
    2024: 68500,
    2025: 71300,  # Official: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contribution-rates-maximums-exemptions.html
    2026: 74600,  # Official: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contribution-rates-maximums-exemptions.html
}

# Mandatory LIF-conversion backstop: a CRI/LIRA must be transferred to a
# LIF/FRV (or used to buy a life annuity) by December 31 of the year the
# owner turns 71. Sourced for Quebec: Retraite Québec, "Caractéristiques
# d'un CRI" — "elle doit transférer le solde du CRI ... au plus tard le 31
# décembre de l'année où elle atteint 71 ans". The same end-of-year-71
# backstop is the Canada-wide norm (ITA s.146(1) "locked-in RRSP" maturity).
MANDATORY_CONVERSION_AGE = 71

# Earliest age at which a CRI/LIRA MAY be converted to a LIF/FRV, by
# jurisdiction. None = no statutory minimum (conversion permitted at any
# age). An early-conversion election (a conversion_date earlier than the
# age-71 backstop) is honoured down to this age; below it the election is
# rejected loudly rather than silently clamped (DP#32: absence/guess must
# fail, never default to a favourable value).
#
# Sourced:
#   Quebec: Retraite Québec, "Caractéristiques d'un CRI" — "Il n'y a pas
#     d'âge minimum pour faire un tel transfert" ("There is no minimum age
#     to make such a transfer"), i.e. a CRI may be transferred to a FRV at
#     any age. The FRV's own under-55 prescribed-rate maximum (modelled in
#     lif_maximum_withdrawal) is consistent with conversion before 55.
#     https://www.retraitequebec.gouv.qc.ca/fr/professionnels-et-employeurs/professionnels-concernes-regimes-retraite/cri-et-frv/caracteristiques-cri
#
# NOT SOURCED (flagged, not guessed — DP#32): the earliest-permitted LIF
# conversion age for federal (PBSA/PBSR) and Ontario (PBA/FSRA) is not
# sourced here. An early-conversion election for those jurisdictions is
# REJECTED (lif_conversion_year raises) rather than guessed; the age-71
# backstop still applies. Open an issue to source the rule before relying
# on an early conversion outside Quebec.
_NOT_SOURCED = object()  # sentinel: a jurisdiction's earliest age is unsourced
EARLIEST_LIF_CONVERSION_AGE: Dict[str, Optional[int]] = {
    'quebec': None,  # no statutory minimum (sourced: Retraite Québec)
}


# =============================================================================
# Quebec Prescribed Rates — Retraite Québec (DP#12, DP#20)
# =============================================================================

# For Quebec LIF (FRV) holders under age 55, the maximum annual withdrawal
# is calculated as: prescribed_rate × balance.
# The prescribed rate is set by Retraite Québec each January 1.
# Source: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx
QUEBEC_PRESCRIBED_RATES = {
    # NOTE: 2024 uses Schedule 0.6 factor (0.061), a different calculation method
    # than 2025+ which uses the prescribed rate formula. Both are stored here for
    # simplicity but represent different legislative mechanisms.
    # Source: Retraite Québec "Modifications aux FRV à compter de 2025"
    2024: 0.061,    # 6.1% — Schedule 0.6 factor (not prescribed rate)
    2025: 0.06,     # 6.00% — Retraite Québec prescribed rate for 2025
    2026: 0.0625,   # 6.25% — Retraite Québec prescribed rate for 2026
}


# CANSIM V122487 rates used in OSFI LIF maximum withdrawal factor calculation.
# These are the prescribed interest rates for the annuity factor computation.
# Source: OSFI LIF Maximum Withdrawal Factor tables
CANSIM_V122487_RATES = {
    2025: 0.0326,   # 3.26% — November 2024
    2026: 0.0349,   # 3.49% — November 2025
}


def _get_cansim_rate(year: int) -> float:
    """Get CANSIM V122487 rate for the given year.

    Falls back to nearest available year if exact year not found.
    Used in computing LIF maximum withdrawal factors for ages outside
    the factor table.
    """
    if year in CANSIM_V122487_RATES:
        return CANSIM_V122487_RATES[year]
    available = sorted(CANSIM_V122487_RATES.keys())
    if not available:
        return 0.06  # 6% fallback (historical default)
    nearest = min(available, key=lambda y: abs(y - year))
    return CANSIM_V122487_RATES[nearest]


# =============================================================================
# LIF Maximum Withdrawal Factors — OSFI-published data (DP#12)
# =============================================================================

# The LIF maximum withdrawal is calculated per PBSR s.20.1:
#   Maximum = C / F
# where C = LIF balance, F = present value of $1/year annuity payable
# annually in advance until end of year owner turns 90, using the
# CANSIM V122487 rate for the first 15 years and 6% thereafter.
#
# OSFI publishes the resulting maximum percentage factors by age and year.
# Store these as year-versioned data dicts (DP#12, DP#20).

LIF_MAX_WITHDRAWAL_FACTORS = {
    # Format: {year: {age: max_percentage_factor}}
    # Source: OSFI LIF Maximum Withdrawal Factor tables
    # https://www.osfi-bsif.gc.ca/en/supervision/regulated-entities/pension-plans/lif-maximum-withdrawal
    #
    # Each year's factors are independently verified against OSFI-published data.
    # Ages 20-54: computed from C/F annuity formula per PBSR s.20.1 using the
    # CANSIM V122487 rate (1/ä_{90-age} at the prescribed rate). These match
    # the OSFI-published values for ages 55-89 in the same year.
    # OSFI publishes the current year and prior year only; earlier years are
    # not available from the official source and are therefore omitted.
    #
    # OSFI table ends at age 89 (factor = 100%); ages 90+ can withdraw entire
    # balance (handled in lif_maximum_withdrawal, not stored here).

    # 2025 — CANSIM V122487 Nov 2024 rate: 3.26%
    # Source: https://www.osfi-bsif.gc.ca/en/supervision/pensions/administering-pension-plans/guidance-topic/life-income-funds-restricted-life-income-funds-variable-benefits-accounts
    2025: {
        # Ages 20-54: computed from C/F annuity formula per PBSR s.20.1
        # using CANSIM V122487 rate for 2025 (3.26%): 1/ä_{90-age} at i=3.26%
        20: 0.035309, 21: 0.035446, 22: 0.035588, 23: 0.035736, 24: 0.035891,
        25: 0.036051, 26: 0.036219, 27: 0.036394, 28: 0.036576, 29: 0.036766,
        30: 0.036964, 31: 0.037171, 32: 0.037387, 33: 0.037613, 34: 0.037849,
        35: 0.038096, 36: 0.038355, 37: 0.038625, 38: 0.038909, 39: 0.039206,
        40: 0.039517, 41: 0.039844, 42: 0.040188, 43: 0.040548, 44: 0.040928,
        45: 0.041327, 46: 0.041748, 47: 0.042191, 48: 0.042659, 49: 0.043153,
        50: 0.043675, 51: 0.044228, 52: 0.044814, 53: 0.045435, 54: 0.046095,
        # Ages 55-89: OSFI-published factors
        55: 0.050987, 56: 0.051524, 57: 0.052105, 58: 0.052736, 59: 0.053421,
        60: 0.054168, 61: 0.054982, 62: 0.055872, 63: 0.056848, 64: 0.057920,
        65: 0.059101, 66: 0.060407, 67: 0.061856, 68: 0.063469, 69: 0.065275,
        70: 0.067303, 71: 0.069597, 72: 0.072204, 73: 0.075190, 74: 0.078638,
        75: 0.082655, 76: 0.087258, 77: 0.092582, 78: 0.098807, 79: 0.106178,
        80: 0.115041, 81: 0.125892, 82: 0.139476, 83: 0.156966, 84: 0.180313,
        85: 0.213033, 86: 0.262155, 87: 0.344082, 88: 0.508019, 89: 1.000000,
    },

    # 2026 — CANSIM V122487 Nov 2025 rate: 3.49%
    # Source: https://www.osfi-bsif.gc.ca/en/supervision/pensions/administering-pension-plans/guidance-topic/life-income-funds-restricted-life-income-funds-variable-benefits-accounts
    2026: {
        # Ages 20-54: computed from C/F annuity formula per PBSR s.20.1
        # using CANSIM V122487 rate for 2026 (3.49%): 1/ä_{90-age} at i=3.49%
        20: 0.037083, 21: 0.037212, 22: 0.037347, 23: 0.037487, 24: 0.037634,
        25: 0.037787, 26: 0.037947, 27: 0.038113, 28: 0.038287, 29: 0.038469,
        30: 0.038659, 31: 0.038857, 32: 0.039065, 33: 0.039282, 34: 0.039509,
        35: 0.039747, 36: 0.039997, 37: 0.040258, 38: 0.040532, 39: 0.040820,
        40: 0.041122, 41: 0.041439, 42: 0.041773, 43: 0.042123, 44: 0.042493,
        45: 0.042882, 46: 0.043292, 47: 0.043725, 48: 0.044183, 49: 0.044666,
        50: 0.045178, 51: 0.045720, 52: 0.046295, 53: 0.046905, 54: 0.047554,
        # Ages 55-89: OSFI-published factors
        55: 0.052096, 56: 0.052637, 57: 0.053224, 58: 0.053861, 59: 0.054552,
        60: 0.055304, 61: 0.056125, 62: 0.057022, 63: 0.058005, 64: 0.059084,
        65: 0.060272, 66: 0.061586, 67: 0.063042, 68: 0.064662, 69: 0.066474,
        70: 0.068508, 71: 0.070804, 72: 0.073413, 73: 0.076397, 74: 0.079836,
        75: 0.083837, 76: 0.088423, 77: 0.093729, 78: 0.099935, 79: 0.107287,
        80: 0.116128, 81: 0.126955, 82: 0.140512, 83: 0.157970, 84: 0.181280,
        85: 0.213952, 86: 0.263008, 87: 0.344831, 88: 0.508575, 89: 1.000000,
    },
}


# =============================================================================
# Hardship / Unlocking Categories — province-specific (DP#12, DP#20)
# =============================================================================

# Recognized unlocking categories across all jurisdictions (superset).
HARDSHIP_CATEGORIES = ('low_income', 'medical', 'eviction', 'non_resident', 'shortened_life')

# Province-specific eligibility for unlocking a locked-in account.
# Each jurisdiction recognizes a different subset of grounds; this differs by
# the governing pension legislation (federal PBSA/PBSR vs. provincial acts).
# Sources:
#   Federal PBSR s.20-20.3 (financial hardship is NOT a federal ground; the
#     federal grounds are small-balance, non-residency 2+ years, shortened
#     life expectancy, and age 55+ one-time 50% transfer):
#     https://laws-lois.justice.gc.ca/eng/regulations/SOR-87-19/
#   Retraite Québec — temporary income / shortened-life / non-resident grounds
#     (Quebec has NO general financial-hardship unlocking for low income/eviction):
#     https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/cri.aspx
#   Ontario FSRA — financial hardship unlocking (low income, medical, arrears):
#     https://www.fsrao.ca/consumers/pensions/financial-hardship-unlocking
JURISDICTION_UNLOCK_CATEGORIES = {
    # Federal (PBSA/PBSR): no financial-hardship grounds; only the
    # statutory unlocking circumstances.
    'federal': ('non_resident', 'shortened_life'),
    # Ontario (PBA/FSRA): full financial-hardship unlocking plus statutory.
    'ontario': ('low_income', 'medical', 'eviction', 'non_resident', 'shortened_life'),
    # Quebec (Retraite Québec): shortened life expectancy and non-residency;
    # Quebec does not offer general low-income/eviction hardship unlocking.
    'quebec': ('non_resident', 'shortened_life'),
}


# =============================================================================
# Pure Functions — Conversion Gate (DP#28)
# =============================================================================

def must_convert_by_year(birth_year: int) -> int:
    """Year by which CRI/LIRA must be converted to LIF — the mandatory backstop.

    December 31 of the year the owner turns 71 (MANDATORY_CONVERSION_AGE).
    This is the LATEST the LIRA may convert; ``lif_conversion_year`` honours
    an earlier election down to the jurisdiction's earliest-permitted age.

    DP#1: compute from birth_year, not stored age.
    DP#28: date-computed gate.
    """
    if birth_year <= 0:
        raise ValueError(f"Invalid birth_year={birth_year}: must be a valid year of birth")
    return birth_year + MANDATORY_CONVERSION_AGE


def lif_conversion_year(birth_year: int, jurisdiction: str,
                        election_year: Optional[int] = None) -> int:
    """Calendar year the CRI/LIRA converts to a LIF/FRV (issue #708).

    The conversion is event-driven, not a hardcoded age (DP#28): the LIRA
    converts at the EARLIER of the household's elected conversion date and
    the mandatory age-71 backstop.

    - ``election_year is None`` (no early-conversion election, the default):
      returns the mandatory backstop (``must_convert_by_year``). Behaviour
      is byte-identical to the pre-#708 path — an absent election cannot
      move any existing trajectory.
    - ``election_year`` supplied: the LIRA converts at
      ``min(election_year, backstop)``. An election cannot delay conversion
      past the backstop. An election earlier than the backstop is an EARLY
      conversion, permitted only down to the jurisdiction's earliest-
      permitted conversion age (``EARLIEST_LIF_CONVERSION_AGE``):

      * Quebec (sourced — Retraite Québec): no minimum age, so any early
        election is honoured (e.g. convert at retirement, age 55/60/65).
      * Federal/Ontario (NOT sourced): an early election is REJECTED (raise)
        rather than guessed. The age-71 backstop still applies if no early
        election is made.

    Args:
        birth_year: Owner's year of birth (DP#1; the owner's birth_date is
            the source of truth — birth_year is its year component).
        jurisdiction: 'federal', 'ontario', or 'quebec'.
        election_year: The calendar year the household elected to convert
            (e.g. derived from ``lira.conversion_date``), or None for "no
            early election — use the backstop."

    Returns:
        The calendar year in which the LIRA converts to a LIF.
    """
    if birth_year <= 0:
        raise ValueError(
            f"Invalid birth_year={birth_year}: must be a valid year of birth")
    backstop = must_convert_by_year(birth_year)
    if election_year is None:
        return backstop
    if election_year <= 0:
        raise ValueError(
            f"Invalid election_year={election_year}: a conversion election "
            f"must be a calendar year > 0 (pass None for no early election).")
    convert_year = min(election_year, backstop)
    if convert_year < backstop:
        # Early conversion: enforce the jurisdiction's earliest-permitted age.
        earliest_age = EARLIEST_LIF_CONVERSION_AGE.get(jurisdiction, _NOT_SOURCED)
        if earliest_age is _NOT_SOURCED:
            raise ValueError(
                f"Earliest LIF-conversion age for jurisdiction "
                f"{jurisdiction!r} is not sourced; cannot honour an early "
                f"conversion election (year {election_year}, age "
                f"{election_year - birth_year}). The age-71 mandatory backstop "
                f"still applies. Open an issue to source the rule, or omit "
                f"conversion_date to use the backstop. (Quebec is sourced: "
                f"no minimum — Retraite Québec.)")
        if earliest_age is not None:
            earliest_year = birth_year + earliest_age
            if convert_year < earliest_year:
                raise ValueError(
                    f"conversion election (year {election_year}, age "
                    f"{convert_year - birth_year}) is before the earliest "
                    f"permitted LIF-conversion age {earliest_age} for "
                    f"jurisdiction {jurisdiction!r}; convert at age >= "
                    f"{earliest_age} (year >= {earliest_year}).")
    return convert_year


# =============================================================================
# Pure Functions — LIF Withdrawal Rules
# =============================================================================

def lif_minimum_withdrawal(balance: float, age: int,
                           rates: Dict = None,
                           year: int = 2026) -> float:
    """LIF minimum withdrawal — same factors as RRIF.

    The LIF minimum withdrawal percentages are identical to the
    RRIF minimum withdrawal factors prescribed by CRA (ITA Reg. 7301).
    For ages not in the table, compute 1/(90-age) per CRA RC4167.
    """
    if rates is None:
        rates = _get_rrif_rates(year=year)
    rate = rates.get(age)
    if rate is None:
        # ITA Reg. 7301: for ages not in the table, use 1/(90-age)
        # For ages >= 90, use the table's highest rate (95: 0.1129)
        # For ages below the table (e.g., < 55), compute 1/(90-age)
        if age >= max(rates.keys()):
            rate = rates[max(rates.keys())]  # Use highest age's rate
        else:
            rate = 1.0 / (90 - age)
    return balance * rate


def lif_maximum_withdrawal(balance: float, age: int, year: int,
                            factors: Dict = None,
                            jurisdiction: str = 'federal',
                            temporary_income: float = 0) -> float:
    """LIF maximum withdrawal — C/F actuarial formula per PBSR s.20.1.

    Maximum = C / F where:
      C = LIF balance at beginning of year
      F = present value of $1/year annuity payable in advance until
          end of year owner turns 90, using CANSIM V122487 rate for
          first 15 years and 6% thereafter.

    OSFI publishes the resulting maximum percentage factors by age
    and year. This function looks up those published factors (DP#12).

    Quebec rules:
    - Ages 55+ (2025+): no maximum — returns balance
    - Ages under 55: prescribed_rate × balance - temporary_income
      Per Retraite Québec Article 20, the maximum life income for
      under-55 FRV holders is reduced by any temporary income
      (revenue temporaire). When temporary_income=0 (default),
      the formula reduces to prescribed_rate × balance.

    Args:
        balance: LIF balance at start of year
        age: Owner's age at start of year
        year: Calendar year (for year-versioned factors, DP#20)
        factors: Custom factor table (default: OSFI LIF max factors)
        jurisdiction: 'federal', 'ontario', or 'quebec' (affects withdrawal rules)
        temporary_income: Quebec under-55 temporary income deduction
            (default 0; per Retraite Québec Article 20)

    Returns:
        Maximum withdrawal amount
    """
    # Quebec removed LIF maximum for ages 55+ effective January 1, 2025
    if jurisdiction == 'quebec' and age >= 55 and year >= 2025:
        return balance

    # Quebec under-55: prescribed_rate × balance - temporary_income
    # Source: Retraite Québec Article 20 — for FRV holders under 55,
    # the maximum life income is: (prescribed_rate × balance) - temporary_income.
    # When no temporary income is claimed, this reduces to prescribed_rate × balance.
    # https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx
    if jurisdiction == 'quebec' and age < 55:
        rate = QUEBEC_PRESCRIBED_RATES.get(year)
        if rate is None:
            available = sorted(QUEBEC_PRESCRIBED_RATES.keys())
            nearest = min(available, key=lambda y: abs(y - year))
            rate = QUEBEC_PRESCRIBED_RATES[nearest]
        return max(0.0, balance * rate - temporary_income)

    if factors is None:
        factors = LIF_MAX_WITHDRAWAL_FACTORS

    year_factors = factors.get(year)
    if year_factors is None:
        # Fallback: use nearest available year in current factor table
        available_years = sorted(factors.keys())
        if available_years:
            nearest = min(available_years, key=lambda y: abs(y - year))
            year_factors = factors[nearest]
        elif factors is not LIF_MAX_WITHDRAWAL_FACTORS:
            # Jurisdiction-specific table is empty (e.g., Quebec 2024).
            # Fall back to federal OSFI factors (documented as underestimating
            # Quebec maximums for ages 55+ before 2025).
            year_factors = LIF_MAX_WITHDRAWAL_FACTORS.get(year)
            if year_factors is None:
                available_years = sorted(LIF_MAX_WITHDRAWAL_FACTORS.keys())
                nearest = min(available_years, key=lambda y: abs(y - year))
                year_factors = LIF_MAX_WITHDRAWAL_FACTORS[nearest]

    factor = year_factors.get(age)
    if factor is None:
        if age >= 89:
            return balance
        # Ages outside the factor table: compute from annuity formula
        # per PBSR s.20.1 (1/ä_{90-age} at prescribed rate)
        rate = _get_cansim_rate(year)
        n = 90 - age
        if n <= 0 or rate <= 0:
            return balance
        v = 1 / (1 + rate)
        d = rate / (1 + rate)
        annuity = (1 - v**n) / d
        factor = 1 / annuity

    return min(balance, balance * factor)


def small_balance_unlock_eligible(balance: float, ympe: float,
                                    birth_year: int, current_year: int,
                                    jurisdiction: str = 'federal') -> bool:
    """Check if small-balance unlock is permitted.

    Federal: balance < 50% YMPE and age >= 55
    Ontario: balance < 40% YMPE and age >= 55
    Quebec: balance < 50% YMPE and age >= 55

    DP#28: age is date-computed from birth_year.

    Args:
        balance: Current CRI/LIRA balance
        ympe: Year's Maximum Pensionable Earnings
        birth_year: Owner's birth year (DP#1)
        current_year: Current calendar year
        jurisdiction: 'federal', 'ontario', or 'quebec'

    Returns:
        True if eligible for small-balance unlock
    """
    age = current_year - birth_year
    if birth_year == 0 or age < 0 or age > 130:
        raise ValueError(
            f"Invalid birth_year={birth_year}: "
            f"age={age}. birth_year must be a valid year of birth.")
    if age < 55:
        return False

    threshold_pct = 0.40 if jurisdiction == 'ontario' else 0.50
    threshold = ympe * threshold_pct
    return 0 < balance < threshold


def hardship_withdrawal_eligible(category: str, jurisdiction: str = None) -> bool:
    """Check if an unlocking category is recognized for a jurisdiction.

    Recognized categories for locked-in account access:
    - low_income: income below threshold (provincial hardship; e.g. Ontario)
    - medical: medical expenses or disability (provincial hardship; e.g. Ontario)
    - eviction: risk of eviction for rent arrears (provincial hardship; e.g. Ontario)
    - non_resident: left Canada permanently (federal + Quebec + provinces)
    - shortened_life: shortened life expectancy (federal + Quebec + provinces)

    When `jurisdiction` is provided, the category must be permitted under that
    jurisdiction's pension legislation (DP#20): federal (PBSA/PBSR) and Quebec
    recognize only non-residency and shortened life expectancy, while Ontario
    additionally recognizes financial-hardship grounds. When `jurisdiction` is
    None, any category in the cross-jurisdiction superset is accepted (legacy).

    The caller is responsible for validating the specific conditions
    of each category (income thresholds, medical documentation, etc.).

    Args:
        category: Hardship/unlocking category string
        jurisdiction: 'federal', 'ontario', or 'quebec' (optional)

    Returns:
        True if the category is recognized for the jurisdiction
    """
    if jurisdiction is None:
        return category in HARDSHIP_CATEGORIES
    return category in JURISDICTION_UNLOCK_CATEGORIES.get(jurisdiction, ())


def lira_to_rrsp_transfer_eligible(balance: float, ympe: float,
                                   birth_year: int, current_year: int,
                                   jurisdiction: str = 'federal',
                                   shortened_life: bool = False) -> bool:
    """Check if the locked-in balance may be transferred to an RRSP/RRIF.

    Unlike the small-balance *cash* unlock (which pays a lump sum), a
    LIRA→RRSP transfer moves the funds tax-deferred into an unlocked RRSP/RRIF,
    removing the locking-in restriction. The grounds mirror the small-balance
    unlock plus shortened life expectancy.

    Grounds:
    - Small balance: balance below the jurisdiction threshold and age >= 55
      (federal: < 50% YMPE; Ontario: < 40% YMPE). DP#20.
    - Shortened life expectancy: a physician-certified shortened life
      expectancy unlocks at any age and may be transferred to an RRSP/RRIF.

    Sources:
        Federal PBSR s.20(1)(d) small-balance & s.20(1)(b) shortened life:
        https://laws-lois.justice.gc.ca/eng/regulations/SOR-87-19/
        Retraite Québec — shortened life expectancy:
        https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/cri.aspx

    Args:
        balance: Current CRI/LIRA balance
        ympe: Year's Maximum Pensionable Earnings
        birth_year: Owner's birth year (DP#1)
        current_year: Current calendar year (DP#28)
        jurisdiction: 'federal', 'ontario', or 'quebec'
        shortened_life: True if a physician-certified shortened life expectancy

    Returns:
        True if eligible to transfer the locked-in balance to an RRSP/RRIF
    """
    if balance <= 0:
        return False
    if shortened_life:
        return True
    # Otherwise the small-balance ground applies (age 55+ + threshold).
    return small_balance_unlock_eligible(
        balance, ympe, birth_year, current_year, jurisdiction)


def death_benefit_disposition(balance: float, has_spouse: bool,
                              spouse_waived: bool = False) -> Dict:
    """Determine how a CRI/LIRA (or LIF) balance is paid out on death.

    On the holder's death the funds cease to be locked in. The disposition
    depends on whether there is a surviving spouse:

    - Surviving spouse (no valid waiver): the balance is paid to the spouse,
      who may transfer it tax-deferred to their own RRSP/RRIF (no immediate
      tax). In Quebec this spousal entitlement takes precedence over the will.
    - No spouse, or the spouse has validly waived/renounced the benefit: the
      balance is paid to the designated beneficiary or the estate as a taxable
      lump sum (included in the deceased's final return).

    Sources:
        Retraite Québec — Death benefit from a LIRA or LIF (spouse priority,
        precedence over the will):
        https://www.retraitequebec.gouv.qc.ca/en/deces/rentes-prestations/Pages/cri-frv.aspx
        Federal PBSA s.23 (survivor entitlement on death):
        https://laws-lois.justice.gc.ca/eng/acts/P-7.01/

    Args:
        balance: Account balance at death
        has_spouse: Whether there is a surviving spouse
        spouse_waived: Whether the spouse validly waived/renounced the benefit

    Returns:
        Dict with:
            recipient: 'spouse' or 'estate'
            amount: balance paid out
            tax_deferred: True if a tax-free rollover to spouse's RRSP/RRIF
                is available; False if paid as a taxable lump sum
            taxable_lump_sum: amount taxed in the deceased's final return
                (0 when rolled over to the spouse)
    """
    if has_spouse and not spouse_waived:
        return {
            'recipient': 'spouse',
            'amount': balance,
            'tax_deferred': True,
            'taxable_lump_sum': 0.0,
        }
    return {
        'recipient': 'estate',
        'amount': balance,
        'tax_deferred': False,
        'taxable_lump_sum': balance,
    }


def compute_lif_withdrawal_range(balance: float, age: int, year: int,
                                   jurisdiction: str = 'federal',
                                   temporary_income: float = 0) -> Dict:
    """Compute the allowed LIF withdrawal range for a year.

    Args:
        balance: LIF balance at start of year
        age: Owner's age at start of year
        year: Calendar year
        jurisdiction: 'federal' or 'quebec'
        temporary_income: Quebec under-55 temporary income deduction (default 0)

    Returns:
        Dict with minimum, maximum, and balance
    """
    minimum = lif_minimum_withdrawal(balance, age)
    maximum = lif_maximum_withdrawal(balance, age, year,
                                      jurisdiction=jurisdiction,
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
        'jurisdiction': jurisdiction,
    }


def get_ympe(year: int) -> int:
    """Get YMPE for a given year.

    DP#12, DP#20: YMPE is year-versioned data.

    Args:
        year: Calendar year

    Returns:
        YMPE for that year, or nearest available year
    """
    if year in YMPE_BY_YEAR:
        return YMPE_BY_YEAR[year]
    available = sorted(YMPE_BY_YEAR.keys())
    nearest = min(available, key=lambda y: abs(y - year))
    return YMPE_BY_YEAR[nearest]


# =============================================================================
# CRI / LIRA Account — Accumulation Phase
# =============================================================================

@dataclass(frozen=True)
class LockedInAccount:
    """CRI (Quebec) / LIRA (federal) locked-in retirement account.

    Holds pension funds transferred from employer pension plans.
    No withdrawals except permitted circumstances (hardship, small-balance
    unlock, non-resident, shortened life expectancy, death).
    No new contributions — balance grows from investment returns only.

    Immutable: all mutating operations return (result, new_instance).

    DP#3: pure functions, no hidden state.
    DP#4: role-based names, not person names.
    DP#26: simulation step is a pure function over explicit state.
    DP#28: eligibility is date-computed from birth_year.
    """
    balance: float = 0.0
    birth_year: int = 0  # DP#1: store date, not age; DP#13: no person-specific defaults
    transfer_date: Optional[str] = None
    source_pension_plan: Optional[str] = None
    creditor_protected: bool = True
    jurisdiction: str = 'federal'  # 'federal', 'ontario', or 'quebec'

    def age_in(self, year: int) -> int:
        """DP#1: compute age from birth_year. Raises ValueError for invalid birth_year."""
        age = year - self.birth_year
        if self.birth_year == 0 or age < 0 or age > 130:
            raise ValueError(
                f"Invalid birth_year={self.birth_year}: "
                f"age_in({year})={age}. birth_year must be a valid year of birth.")
        return age

    def age_for_factor_lookup(self, year: int) -> int:
        """Age on December 31 of previous year for OSFI/CRA factor table lookups.

        OSFI LIF max tables and CRA RRIF min tables use \"age at start of year\"
        (= age on Dec 31 of previous year). This differs from age_in(year)
        by 1 for most birth dates.
        """
        return self.age_in(year) - 1

    def must_convert(self, current_year: int) -> bool:
        """Check if conversion to LIF is mandatory this year.

        DP#28: date-computed gate from birth_year.
        """
        self.age_in(current_year)  # validate birth_year
        return current_year >= must_convert_by_year(self.birth_year)

    def contribute(self, amount: float) -> Tuple[float, 'LockedInAccount']:
        """CRI/LIRA does not accept new contributions.

        Returns (0, self) — no state change.
        """
        return 0.0, self

    def grow(self, return_rate: float) -> Tuple[float, 'LockedInAccount']:
        """Apply investment growth. Returns (growth_amount, new_account).

        DP#9: shares countries/canada/account_models._apply_growth — this
        was byte-identical to LIFFund.grow (and to the mutable-style grow()
        methods in account_models.py) before dedup.
        """
        new_balance, growth = _apply_growth(self.balance, return_rate)
        return growth, replace(self, balance=new_balance)

    def withdraw(self, amount: float) -> Tuple[float, 'LockedInAccount']:
        """Normal withdrawal from CRI/LIRA — blocked.

        Returns (0, self) — no state change.
        """
        return 0.0, self

    def hardship_withdrawal(self, amount: float,
                             category: str = None) -> Tuple[float, 'LockedInAccount']:
        """Withdraw under financial hardship provision.

        If category is provided, validates it against recognized
        hardship categories. The caller is responsible for validating
        specific eligibility conditions (income thresholds, documentation).

        Args:
            amount: Requested withdrawal amount
            category: Hardship category (optional, validated if provided)

        Returns:
            (actual_withdrawn, new_account)
        """
        if category is not None and not hardship_withdrawal_eligible(
            category, jurisdiction=self.jurisdiction
        ):
            return 0.0, self

        actual = min(amount, self.balance)
        return actual, replace(self, balance=self.balance - actual)

    def transfer_to_rrsp(
        self, ympe: float, current_year: int, shortened_life: bool = False
    ) -> Tuple[float, 'LockedInAccount']:
        """Transfer the locked-in balance to an RRSP/RRIF when eligible.

        Unlocks the full balance tax-deferred (the funds leave the locked-in
        account for an unlocked RRSP/RRIF). Returns (amount_transferred,
        new_account); a depleted account when the transfer is permitted, or
        (0, self) when it is not.

        Eligibility uses the small-balance ground (age 55+ and jurisdiction
        threshold) or a physician-certified shortened life expectancy.

        Args:
            ympe: Year's Maximum Pensionable Earnings
            current_year: Current calendar year (DP#28)
            shortened_life: True if shortened life expectancy is certified
        """
        self.age_in(current_year)  # validate birth_year
        if not lira_to_rrsp_transfer_eligible(
            self.balance, ympe, self.birth_year, current_year,
            self.jurisdiction, shortened_life
        ):
            return 0.0, self
        return self.balance, replace(self, balance=0)

    def death_disposition(
        self, has_spouse: bool, spouse_waived: bool = False
    ) -> Tuple[Dict, 'LockedInAccount']:
        """Dispose of the balance on the holder's death (DP#10).

        Returns (disposition, depleted_account). The disposition dict reports
        the recipient (spouse vs estate) and whether the payout rolls over
        tax-deferred to the spouse's RRSP/RRIF or is a taxable lump sum to the
        estate. The account is left with a zero balance.
        """
        disposition = death_benefit_disposition(
            self.balance, has_spouse, spouse_waived)
        return disposition, replace(self, balance=0)

    def small_balance_unlock_with_jurisdiction(
        self, ympe: float, current_year: int, jurisdiction: str = 'federal'
    ) -> Tuple[float, 'LockedInAccount']:
        """Unlock small balance with jurisdiction-specific threshold.

        Args:
            ympe: Year's Maximum Pensionable Earnings
            current_year: Current calendar year (DP#28)
            jurisdiction: 'federal', 'ontario', or 'quebec'

        Returns:
            (amount_unlocked, new_account)
        """
        self.age_in(current_year)  # validate birth_year
        if not small_balance_unlock_eligible(
            self.balance, ympe, self.birth_year, current_year, jurisdiction
        ):
            return 0.0, self
        return self.balance, replace(self, balance=0)

    def seizable_amount(self) -> float:
        """Creditor-protected: balance is not seizable."""
        return 0.0

    def convert_to_lif(self, year: int,
                        reference_rate: float = 0.06) -> Tuple['LIFFund', 'LockedInAccount']:
        """Convert CRI/LIRA to LIF.

        Mandatory by December 31 of the year the owner turns 71.
        May convert earlier for pension splitting eligibility (65+).

        DP#3: returns (lif, depleted_account) — no mutation.
        A Quebec CRI/LIRA converts to a Quebec LIF (FRV), preserving
        jurisdiction so withdrawal rules apply correctly.

        Args:
            year: Year of conversion (required, DP#13)
            reference_rate: CILR/QMFR reference rate for LIF maximum

        Returns:
            (LIFFund, LockedInAccount_with_zero_balance)
        """
        self.age_in(year)  # validate birth_year first
        must_convert_year = must_convert_by_year(self.birth_year)
        converted_late = year > must_convert_year

        lif = LIFFund(
            balance=self.balance,
            owner_birth_year=self.birth_year,
            reference_rate=reference_rate,
            converted_late=converted_late,
            jurisdiction=self.jurisdiction,
        )
        depleted = replace(self, balance=0)
        return lif, depleted

    @classmethod
    def from_dict(cls, data: dict) -> 'LockedInAccount':
        """Create from input config dict. DP#24: round-trip."""
        birth_year = data.get('birth_year', 0)
        # Coerce None (from schema's nullable field) to sentinel 0
        # age_in() will raise ValueError when birth_year=0 is used
        if birth_year is None:
            birth_year = 0
        return cls(
            balance=data.get('balance', 0),
            birth_year=birth_year,
            transfer_date=data.get('transfer_date'),
            source_pension_plan=data.get('source_pension_plan'),
            creditor_protected=data.get('creditor_protected', True),
            jurisdiction=data.get('jurisdiction', 'federal'),
        )

    def to_dict(self) -> dict:
        """Export to dict. DP#24: round-trip."""
        return {
            'balance': self.balance,
            'birth_year': self.birth_year,
            'transfer_date': self.transfer_date,
            'source_pension_plan': self.source_pension_plan,
            'creditor_protected': self.creditor_protected,
            'jurisdiction': self.jurisdiction,
        }


# =============================================================================
# LIF — Decumulation Phase
# =============================================================================

@dataclass(frozen=True)
class LIFFund:
    """Life Income Fund (LIF / FRV).

    Created by converting a CRI/LIRA at or before age 71.
    LIF has both minimum and maximum annual withdrawals.
    Minimums match RRIF factors; maximums use C/F actuarial formula
    per PBSR s.20.1 (OSFI-published factors).

    Immutable: all mutating operations return (result, new_instance).

    DP#3: pure functions, no hidden state.
    DP#26: simulation step is a pure function over explicit state.
    """
    balance: float = 0.0
    owner_birth_year: int = 0  # DP#1; DP#13: no person-specific defaults
    reference_rate: float = 0.06  # CILR/QMFR (DP#13: fallback default)
    converted_late: bool = False
    jurisdiction: str = 'federal'

    def age_in(self, year: int) -> int:
        """DP#1: compute age from birth_year."""
        age = year - self.owner_birth_year
        if self.owner_birth_year == 0 or age < 0 or age > 130:
            raise ValueError(
                f"Invalid birth_year={self.owner_birth_year}: "
                f"age_in({year})={age}. birth_year must be a valid year of birth.")
        return age

    def age_for_factor_lookup(self, year: int) -> int:
        """Age on December 31 of previous year for OSFI/CRA factor table lookups."""
        return self.age_in(year) - 1

    def minimum_withdrawal(self, year: int) -> float:
        """LIF minimum withdrawal for the given year.

        Same factors as RRIF minimum withdrawal.
        Uses age on Dec 31 of previous year per CRA RC4167.
        """
        age = self.age_for_factor_lookup(year)
        return lif_minimum_withdrawal(self.balance, age)

    def maximum_withdrawal(self, year: int, temporary_income: float = 0) -> float:
        """LIF maximum withdrawal for the given year.

        Uses OSFI-published C/F actuarial factors per PBSR s.20.1.
        Uses age on Dec 31 of previous year per OSFI table convention.
        For Quebec FRV under-55, temporary_income reduces the maximum.
        """
        age = self.age_for_factor_lookup(year)
        return lif_maximum_withdrawal(self.balance, age, year,
                                      jurisdiction=self.jurisdiction,
                                      temporary_income=temporary_income)

    def withdrawal_range(self, year: int, temporary_income: float = 0) -> Dict:
        """Allowed withdrawal range for the year."""
        return compute_lif_withdrawal_range(
            self.balance, self.age_for_factor_lookup(year), year,
            jurisdiction=self.jurisdiction,
            temporary_income=temporary_income)

    def withdraw(self, amount: float, year: int, temporary_income: float = 0) -> Tuple[float, 'LIFFund']:
        """Withdraw from LIF, enforcing min and max bounds.

        Returns (actual_withdrawn, new_fund).

        - Below minimum → forced to minimum
        - Above maximum → capped at maximum
        - Between min and max → allowed as requested
        """
        min_w = self.minimum_withdrawal(year)
        max_w = self.maximum_withdrawal(year, temporary_income=temporary_income)
        # Guard: temporary_income can reduce maximum below minimum for Quebec
        # under-55. When min > max, no forced withdrawal above maximum.
        if min_w > max_w:
            min_w = max_w
        actual = max(min_w, min(amount, max_w))
        actual = min(actual, self.balance)
        return actual, replace(self, balance=self.balance - actual)

    def contribute(self, amount: float) -> Tuple[float, 'LIFFund']:
        """LIF does not accept new contributions."""
        return 0.0, self

    def grow(self, return_rate: float) -> Tuple[float, 'LIFFund']:
        """Apply investment growth. Returns (growth_amount, new_fund).

        DP#9: shares countries/canada/account_models._apply_growth.
        """
        new_balance, growth = _apply_growth(self.balance, return_rate)
        return growth, replace(self, balance=new_balance)

    def withdrawal_tax(self, withdrawal: float, marginal_rate: float) -> float:
        """LIF withdrawals are taxable as regular income."""
        return withdrawal * marginal_rate

    def to_dict(self) -> dict:
        """Export to dict. DP#24: round-trip."""
        return {
            'balance': self.balance,
            'owner_birth_year': self.owner_birth_year,
            'reference_rate': self.reference_rate,
            'converted_late': self.converted_late,
            'jurisdiction': self.jurisdiction,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LIFFund':
        """Create from input config dict. DP#24: round-trip."""
        owner_birth_year = data.get('owner_birth_year', 0)
        # Coerce None (from schema's nullable field) to sentinel 0
        # age_in() will raise ValueError when owner_birth_year=0 is used
        if owner_birth_year is None:
            owner_birth_year = 0
        return cls(
            balance=data.get('balance', 0),
            owner_birth_year=owner_birth_year,
            reference_rate=data.get('reference_rate', 0.06),
            converted_late=data.get('converted_late', False),
            jurisdiction=data.get('jurisdiction', 'federal'),
        )


# =============================================================================
# LIF-conversion provider (DP#25/DP#10, issue #283)
# =============================================================================
# The core simulation layer (simulation_state.py) must not import this
# jurisdiction module. Instead this module exposes a provider implementing the
# core's LIF-conversion contract; countries/canada/__init__.py registers it at
# import time so the dependency points inward (canada → core), never outward.
# The factory methods just forward to the classes/functions above, so the
# behaviour of simulate_year_pure is byte-identical to the prior direct-import
# path.

class CanadaLIFConversionProvider:
    """Canada implementation of the core LIF-conversion contract (issue #283)."""

    @staticmethod
    def must_convert_by_year(birth_year: int) -> int:
        return must_convert_by_year(birth_year)

    @staticmethod
    def lif_conversion_year(birth_year: int, jurisdiction: str,
                            election_year: Optional[int] = None) -> int:
        # Issue #708: date/event-driven conversion (early election honoured
        # down to the jurisdiction's earliest-permitted age; age-71 backstop
        # always applies). See countries.canada.locked_in_account for the
        # sourced rules and the rejection of unsourced-jurisdiction guesses.
        return lif_conversion_year(birth_year, jurisdiction, election_year)

    @staticmethod
    def make_locked_in_account(balance: float, birth_year: int,
                               jurisdiction: str) -> 'LockedInAccount':
        return LockedInAccount(
            balance=balance,
            birth_year=birth_year,
            jurisdiction=jurisdiction,
        )

    @staticmethod
    def make_lif_fund(balance: float, owner_birth_year: int,
                      reference_rate: float, jurisdiction: str) -> 'LIFFund':
        return LIFFund(
            balance=balance,
            owner_birth_year=owner_birth_year,
            reference_rate=reference_rate,
            jurisdiction=jurisdiction,
        )


# Module-level singleton provider (stateless; safe to share).
LIF_CONVERSION_PROVIDER = CanadaLIFConversionProvider()
