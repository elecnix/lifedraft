#!/usr/bin/env python3
"""OSFI B-20 charge geometry, and the typed refusals it protects.

Split out of ``simulation_config.py`` (which had grown to 3,353 lines / 184 KB
and was unreviewable in one piece). This module holds ONE concept: a registered
charge against a property is a single, bounded facility -- the amortizing
mortgage and the revolving HELOC are carved out of it, not independent
borrowing sources -- plus the three ``ValueError`` subclasses raised when a
config would breach that geometry rather than being silently simulated.

Pure arithmetic and exception types: no imports, no config-shape knowledge.
"""

# ── Issue #664: the charge as a first-class concept ─────────────────────────
#
# On a readvanceable/all-in-one mortgage (Manulife One, Scotia STEP, BNC
# All-in-One), the amortizing mortgage and the revolving HELOC are carved out
# of ONE registered charge against the property with ONE combined limit. They
# are not independent borrowing sources -- paying the mortgage down is what
# *creates* HELOC room; drawing the HELOC is what *consumes* it.
#
# OSFI's "Clarification on the Treatment of Innovative Real Estate Secured
# Lending Products under Guideline B-20" (Combined Loan Plan guidance,
# osfi-bsif.gc.ca) states: the overall CLP limit (amortizing + revolving)
# cannot exceed 80% LTV -- the legal maximum for an uninsured mortgage. Any
# lending above 65% LTV must be amortizing and non-readvanceable; principal
# payments applied to the segment above 65% must be matched by a reduction in
# the overall authorized limit, until the CLP's combined limit is back down to
# 65% LTV. In other words: the REVOLVING/readvanceable segment on its own is
# capped at 65% LTV, independent of the 80% combined cap.
OSFI_B20_CHARGE_LTV_MAX = 0.80
OSFI_B20_REVOLVING_LTV_MAX = 0.65

# Dollar tolerance for charge-limit comparisons (floating-point rounding, not
# a policy margin).
_CHARGE_TOLERANCE = 0.01


def charge_limit(house_value: float, charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX) -> float:
    """The combined-facility ceiling of ONE registered charge against a
    property (issue #664): mortgage balance + drawn/limit revolving HELOC
    must fit inside this. ``charge_ltv_limit`` is a fallback (DP#13), not an
    opinion -- pass a config's own declared ``charge_ltv_limit`` when one is
    known (e.g. ``charge_limit(config.house_value, config.charge_ltv_limit)``).
    """
    return house_value * charge_ltv_limit


def heloc_revolving_limit(house_value: float, heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX) -> float:
    """The revolving-only ceiling (OSFI B-20): independent of the 80%
    combined cap, the readvanceable/revolving segment alone may not exceed
    65% LTV -- lending between 65% and 80% must be amortizing and
    non-readvanceable.
    """
    return house_value * heloc_ltv_limit


def charge_room_for_readvance(house_value: float,
                               mortgage_balance: float,
                               drawn_revolving: float,
                               charge_ltv_limit: float = OSFI_B20_CHARGE_LTV_MAX,
                               heloc_ltv_limit: float = OSFI_B20_REVOLVING_LTV_MAX) -> float:
    """How much a readvanceable line may advance right now, given the
    charge it shares with the mortgage (issue #681).

    This is THE readvanceable-mortgage mechanism, stated once (DP#7: the
    mechanism, not the branded product; DP#9: once, not per caller). The
    charge registered against the property is FIXED, so paying mortgage
    principal down is what CREATES line room, and drawing the line is what
    CONSUMES it. The line cannot advance past the charge -- when the charge
    is full, the readvance stops (returns 0.0, never negative).

    Two ceilings bind, whichever is lower (OSFI B-20, see the module
    constants):

      - the COMBINED charge (80% LTV): the amortizing mortgage plus the
        drawn revolving balance must fit inside ``charge_limit``;
      - the REVOLVING-only ceiling (65% LTV): the drawn revolving balance
        alone must fit inside ``heloc_revolving_limit``, independent of how
        much combined room the 80% cap leaves -- lending between 65% and 80%
        must be amortizing and non-readvanceable, i.e. it cannot be
        readvanced into the line at all.

    Pure function (DP#3): same inputs, same output; no state, no clamping of
    its own inputs.

    Args:
        house_value: the appraised value the charge is registered against.
        mortgage_balance: the amortizing segment's balance right now.
        drawn_revolving: the revolving segment's DRAWN balance right now
            (SM-readvanced + any personal-draw margin) -- not its limit.
        charge_ltv_limit: combined ceiling as a fraction of house_value.
        heloc_ltv_limit: revolving-only ceiling as a fraction of house_value.

    Returns:
        The advanceable room, in [0, inf).
    """
    combined_room = charge_limit(house_value, charge_ltv_limit) - (mortgage_balance + drawn_revolving)
    revolving_room = heloc_revolving_limit(house_value, heloc_ltv_limit) - drawn_revolving
    return max(0.0, min(combined_room, revolving_room))


class ChargeLimitExceededError(ValueError):
    """DP#32 (issue #664): total secured debt against a property's single
    registered charge exceeds the charge's LTV ceiling. On a readvanceable/
    all-in-one mortgage the mortgage and the HELOC share ONE charge and are
    NOT independent borrowing sources -- refused loudly rather than silently
    modeled as a >100% LTV facility.
    """


class MissingRefinanceAmortizationError(ValueError):
    """DP#32 (issue #655): a cash-out refinance is a NEW LOAN with its own
    amortization. The overlay refuses to silently inherit the incumbent
    mortgage's remaining amortization when no refinance amortization is
    declared or supplied -- doing so overstates the required payment by
    roughly 2x on a near-payoff mortgage.
    """


class ReadvanceableWithoutPropertyError(ValueError):
    """DP#32 (issue #681/#657): the readvanceable-mortgage strategy is a claim
    on a charge registered against a PROPERTY, but no property value was
    declared, so the line's advanceable room is unknowable -- refused loudly.

    This is a TYPED infeasibility, not a bug: when the optimizer's grid sweeps
    ``use_readvanceable=[True, False]`` over a household with no property, the
    ``True`` branch is a legitimately-infeasible sweep point (like an LTV the
    property cannot support). Issue #657: the optimizer catches THIS narrowly
    and reports it as ``is_infeasible`` with a reason, rather than crashing the
    whole run OR swallowing it into a silent ``-inf`` row. A subclass of
    ValueError so the direct FamilySimulation path still fails loud for callers
    that hand-build an impossible config.
    """
