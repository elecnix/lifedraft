"""Quebec refundable tax credit for home-support services for seniors (CIMAD).

Issue #366. The *crédit d'impôt pour maintien à domicile des aînés* — Taxation
Act, RLRQ c. I-3, art. 1029.8.61.1 to 1029.8.61.7.1 — is a REFUNDABLE credit
for a Quebec resident who is 70 or over on December 31 and pays for eligible
home-support services. It is paid in advance on request and does not depend on
tax payable, so it is cash rather than a reduction of tax.

Sources, both read rather than inferred:

* Revenu Québec, line 458 (2025 return) —
  https://www.revenuquebec.ca/fr/citoyens/declaration-de-revenus/produire-votre-declaration-de-revenus/comment-remplir-votre-declaration-de-revenus/aide-par-ligne/451-a-480-remboursement-ou-solde-a-payer/ligne-458/
  "Ce crédit d'impôt est égal à 39 % de vos dépenses admissibles" (2025), the
  five caps, and the 5%-of-rent rule with its $600/$1,200 monthly floor and cap.
* CFFP, "Crédit pour maintien à domicile des aînés" —
  https://cffp.recherche.usherbrooke.ca/outils-ressources/guide-mesures-fiscales/credit-maintien-domicile-aines/
  the year-versioned rate (36/37/38/39/40 for 2022-2026), the two reduction
  thresholds ($71,010 and $115,035 for 2025; $72,465 and $117,395 for 2026), and
  the reduction rates.

WHAT THIS MODULE DELIBERATELY REFUSES, and why each refusal is loud rather than
approximated:

* **A year other than 2025 or 2026.** The 2022-2024 rates are published
  (36/37/38%) but their REDUCTION THRESHOLDS are indexed annually and are not on
  either source above, so pricing 2024 with 2025's thresholds would overstate or
  understate the credit on every middle-income household.
* **A non-autonomous claimant.** The two published summaries disagree about the
  cap on the reduction — the 2025 table says "Réduction maximale : 4 % des
  dépenses admissibles", the 2026 preview says "3 % x dépenses admissibles" —
  and Revenu Québec computes this case on the *grille de calcul 458*, which is
  a form, not a published formula. Picking one of the two would be a guess.
* **A private seniors' residence (RPA) or a condo.** Both derive eligible
  expenses from a landlord's or syndicat's breakdown (RPA calculation tables;
  form TPZ-1029.MD.5 for condo fees) that is not published on these pages.
* **A health establishment.** Only the services NOT provided by the
  establishment qualify, and the module cannot tell them apart from a total.

DP#3: pure functions. DP#10: one module per government program. DP#20: every
rate, cap and threshold is year-versioned data. DP#32: an input the module
cannot price refuses; it is never answered with a plausible zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# DP#20: the rate has risen one point a year since 2022 and stops at 40% in
# 2026. Kept as a year table, not a formula: it is legislation, not arithmetic.
_CREDIT_RATE = {2025: 0.39, 2026: 0.40}

# The annual cap on eligible EXPENSES, by the situation the household
# DECLARES. These have been $19,500 (single) and $39,000 (couple) since 2017
# (the CFFP's "Historique de la mesure") and the 2026 preview repeats both. The
# non-autonomous caps ($25,500 / $45,000 / $51,000) are deliberately NOT
# carried: that path refuses outright, and a constant nothing reads is dead
# data.
#
# The situation is a REQUIRED argument rather than a `couple=False` default:
# a caller who forgot the flag would silently receive the SINGLE cap and
# understate a couple's payment by half, which is the same
# absence-picks-a-value shape this repo bans everywhere else.
_SITUATION_CAP = {
    "single_autonomous": 19_500.0,
    "couple_autonomous": 39_000.0,
}
_NON_AUTONOMOUS_SITUATION = "non_autonomous"

# The family-income reduction, autonomous claimants: 3% of the income above the
# first threshold up to the second, then 7% of everything above the second.
_REDUCTION_RATE_LOW = 0.03
_REDUCTION_RATE_HIGH = 0.07
_REDUCTION_THRESHOLDS = {2025: (71_010.0, 115_035.0), 2026: (72_465.0, 117_395.0)}

# A renter's services share: 5% of the monthly rent, with the rent floored at
# $600 and capped at $1,200 — so $30 to $60 a month, $360 to $720 a year.
_RENT_SERVICES_SHARE = 0.05
_RENT_MONTHLY_FLOOR = 600.0
_RENT_MONTHLY_CAP = 1_200.0

# One refusal text, used by every entry point that can meet the case, so the
# reasoning cannot drift between them.
_NON_AUTONOMOUS_REFUSAL = (
    "The home-support credit for a NON-AUTONOMOUS claimant is refused, not "
    "guessed. Revenu Québec computes that case on the grille de calcul 458, and "
    "the two published summaries disagree on the cap: the 2025 line-458 table "
    "says the reduction is capped at 4% of eligible expenses, while the 2026 "
    "preview says 3% of them. Pricing the autonomous formula for a "
    "non-autonomous household would understate the credit by that difference "
    "(DP#32), so supply the grid's figures before this path is opened."
)


@dataclass(frozen=True)
class HomeSupportCredit:
    """The year's credit, with the two quantities it was priced from.

    ``eligible_expenses`` and ``reduction`` travel with the result so a
    household (and a reviewer) can see WHICH cap bound and WHICH threshold
    started clawing back, rather than only the final number.
    """
    eligible_expenses: float
    credit_before_reduction: float
    reduction: float
    credit: float
    expense_cap: float


def credit_rate(year: int) -> float:
    """The year's credit rate, or a refusal naming the years that are priced."""
    if year not in _CREDIT_RATE:
        raise ValueError(
            f"The home-support credit rate is registered for "
            f"{sorted(_CREDIT_RATE)} and {year} is not one of them. The 2022-2024 "
            f"rates (36%/37%/38%) are published, but their indexed family-income "
            f"reduction thresholds are not on the sources this module cites, so "
            f"pricing {year} would price it on another year's thresholds "
            f"(DP#32). Add the year's rate AND its two thresholds together."
        )
    return _CREDIT_RATE[year]


def autonomous_expense_cap(year: int, situation: str) -> float:
    """The annual cap on eligible expenses for a declared AUTONOMOUS situation.

    ``situation`` is one of ``single_autonomous`` / ``couple_autonomous``, or
    ``non_autonomous``, which refuses (see ``home_support_credit``).
    """
    credit_rate(year)  # same year registration; refuses an unpriced year once
    if situation == _NON_AUTONOMOUS_SITUATION:
        raise ValueError(_NON_AUTONOMOUS_REFUSAL)
    if situation not in _SITUATION_CAP:
        raise ValueError(
            f"Unknown home-support situation {situation!r}. The published table "
            f"has {sorted(_SITUATION_CAP)} for autonomous claimants plus "
            f"{_NON_AUTONOMOUS_SITUATION!r}; anything else has no cap, so there "
            f"is no credit to compute (DP#32)."
        )
    return _SITUATION_CAP[situation]


def eligible_service_expenses_from_rent(monthly_rent: float) -> float:
    """The year's eligible services INSIDE a rental, derived from the rent.

    Revenu Québec (line 458): "5 % du coût de votre loyer mensuel est admissible
    au crédit d'impôt. Le loyer mensuel minimal admissible est de 600 $ ... le
    loyer maximal admissible est de 1 200 $." The floor is a FLOOR, not a
    threshold: a $540 rent still yields the 5% of $600, which is why it is a
    clamp and not a comparison.
    """
    if monthly_rent < 0:
        raise ValueError(
            f"Monthly rent {monthly_rent!r} is negative. Rent is a payment, so "
            f"a negative figure is a bad input rather than a credit-reducing "
            f"one; clamping it to zero would hide the error (DP#32)."
        )
    clamped = min(max(monthly_rent, _RENT_MONTHLY_FLOOR), _RENT_MONTHLY_CAP)
    return clamped * _RENT_SERVICES_SHARE * 12.0


def _autonomous_reduction(year: int, family_income: float) -> float:
    """3% of the income above the first threshold up to the second, then 7%."""
    if year not in _REDUCTION_THRESHOLDS:
        raise ValueError(
            f"The home-support credit's reduction thresholds are registered for "
            f"{sorted(_REDUCTION_THRESHOLDS)} and {year} is not one of them, so "
            f"the reduction cannot be computed for {year} (DP#32)."
        )
    first, second = _REDUCTION_THRESHOLDS[year]
    if family_income <= first:
        return 0.0
    below = min(family_income, second) - first
    above = max(0.0, family_income - second)
    return below * _REDUCTION_RATE_LOW + above * _REDUCTION_RATE_HIGH


def home_support_credit(
    year: int,
    eligible_expenses: float,
    family_income: float,
    *,
    situation: str,
) -> HomeSupportCredit:
    """The Quebec home-support credit for one year, for an AUTONOMOUS claimant.

    ``eligible_expenses`` is what the household actually paid for eligible
    services (or, for a renter, the share derived by
    ``eligible_service_expenses_from_rent``); it is capped by the year's limit
    for the DECLARED ``situation``. ``family_income`` is line 275 of the return
    plus the spouse's, and only drives the reduction.

    Floored at zero: the reduction can extinguish the credit but never turn it
    into an amount owed (DP#32 — this is a payment, not a charge).
    """
    rate = credit_rate(year)
    if situation == _NON_AUTONOMOUS_SITUATION:
        raise ValueError(_NON_AUTONOMOUS_REFUSAL)
    if eligible_expenses < 0:
        raise ValueError(
            f"Eligible home-support expenses {eligible_expenses!r} are negative. "
            f"Reimbursements reduce them (line 458 says so), but a negative "
            f"total means the input is wrong; clamping to zero would hide it "
            f"(DP#32)."
        )
    if family_income < 0:
        raise ValueError(
            f"Family income {family_income!r} is negative, so the reduction "
            f"cannot be computed. A refunded or reversed income is a bad input, "
            f"not a zero (DP#32)."
        )

    cap = autonomous_expense_cap(year, situation)
    eligible = min(eligible_expenses, cap)
    before_reduction = rate * eligible
    reduction = _autonomous_reduction(year, family_income)
    return HomeSupportCredit(
        eligible_expenses=eligible,
        credit_before_reduction=before_reduction,
        reduction=reduction,
        credit=max(0.0, before_reduction - reduction),
        expense_cap=cap,
    )


def renter_credit(
    year: int,
    monthly_rent: float,
    family_income: float,
    *,
    situation: str,
) -> HomeSupportCredit:
    """The credit for a renter in an ordinary rental building.

    Same arithmetic as ``home_support_credit``; the eligible expenses are the
    rent's services share rather than a declared service total.
    """
    return home_support_credit(
        year,
        eligible_service_expenses_from_rent(monthly_rent),
        family_income,
        situation=situation,
    )
