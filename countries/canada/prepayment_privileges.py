#!/usr/bin/env python3
"""Prepayment privileges and the excess-prepayment penalty — issue #113.

A Canadian closed-term mortgage contract grants two privilege mechanisms,
per tranche (from a signed variable-rate mortgage convention):

1. **Lump-sum prepayment privilege**: up to ``annual_lump_sum_pct`` of the
   tranche's ORIGINAL principal per calendar year, penalty-free. Does NOT
   apply when the tranche is repaid in full — a full repayment is breakage,
   and every dollar of it is penalized.
2. **Payment-increase privilege**: at each regular payment the borrower may
   add up to ``payment_increase_multiplier`` × the regular payment amount,
   penalty-free, repeatable at every payment. Separate from (and additional
   to) the annual lump-sum cap.

Any prepayment ABOVE both privileges is charged a penalty of
``penalty_months_interest`` months of interest on the excess, computed at
the lender's **standard variable rate in vigour at the time** — NOT the
borrower's discounted contract rate (the two are different rates and this
module refuses to guess one from the other; DP#32).

Per DP#10: this module owns the prepayment-privilege rules for Canada.
Per DP#3/DP#9: it is the ONE spelling of the allowance split and the
penalty — ``rate_model.amortization_schedule`` calls these functions rather
than re-deriving them, so a reserve-sized price and a schedule-applied
price can never disagree.
Per DP#32: every term is DECLARED data with explicit validation; a missing
standard variable rate is refused loudly, never approximated from the
contract rate (that substitution would understate the penalty by exactly
the discount the borrower negotiated away from the posted rate).

Relationship to the existing breakage model (``ird_penalty.py``):
``compute_breakage_penalty`` prices FULL-tranche breakage (repaying the
entire balance before term end). This module prices PARTIAL prepayments —
the free-vs-penalized split that sits BELOW the breakage threshold — plus
the full-repayment clause (the entire amount penalized) so the two models
agree at the boundary: as the prepayment grows to the whole balance, the
privilege allowances stop applying and the charge converges to 3 months'
interest on the full amount.

Interaction with the #1075 origination cash-back: the cash-back clawback is
contingent on the mortgage being FULLY prepaid before the retention term
elapses. A partial prepayment — even a large penalized one — never triggers
it; only a full repayment does, and a full repayment gets no privilege
protection (clause 1 above), so a free slice can never shield a repayment
that fires the clawback. The two charges stack on a full early repayment:
3 months' interest on the full amount + the declared clawback fraction.

Usage:
    from countries.canada.prepayment_privileges import (
        PrepaymentPrivileges, price_monthly_extra,
    )

    priv = PrepaymentPrivileges(
        original_principal=500_000.0, annual_lump_sum_pct=0.10,
        payment_increase_multiplier=1.0, penalty_months_interest=3,
        standard_variable_rate=0.06,
    )
    split = price_monthly_extra(priv, requested_extra=5_000.0,
                                regular_payment=3_000.0,
                                lump_sum_used_ytd=48_000.0)
    split['excess']          # -> 3_000.0 (the penalized overshoot)
    split['penalty']         # -> 3_000 * 0.06 * 3 / 12 = 45.0
"""

from dataclasses import dataclass

# Tolerance for the closes-the-loan comparison (float schedules).
_BALANCE_EPSILON = 1e-6


# =============================================================================
# Declared terms — validated loudly (DP#32)
# =============================================================================

@dataclass(frozen=True)
class PrepaymentPrivileges:
    """One tranche's declared prepayment privileges and penalty terms.

    Args:
        original_principal: The tranche's ORIGINAL principal — the base the
            annual lump-sum percentage applies to for EVERY year of the
            term (not the declining balance).
        annual_lump_sum_pct: Fraction of original principal prepayable
            penalty-free per calendar year (0.10 = 10%).
        payment_increase_multiplier: How many times the regular payment may
            be added at each regular payment, penalty-free (1.0 = one extra
            payment's worth at each payment; 0.0 = no increase privilege).
        penalty_months_interest: Months of interest charged on the excess
            (typically 3 for a variable-rate closed term).
        standard_variable_rate: The lender's standard variable rate in
            vigour — the rate the penalty is COMPUTED at. Required outright
            when a penalty can arise: defaulting it to the borrower's
            discounted contract rate would silently understate every
            penalty by the discount (DP#32 — absence must fail loudly).
    """

    original_principal: float
    annual_lump_sum_pct: float
    payment_increase_multiplier: float
    penalty_months_interest: float
    standard_variable_rate: float

    def __post_init__(self):
        if self.original_principal <= 0:
            raise ValueError(
                f"PrepaymentPrivileges: original_principal must be > 0 "
                f"(the annual lump-sum allowance is a percentage OF it), "
                f"got {self.original_principal}."
            )
        if not (0.0 <= self.annual_lump_sum_pct <= 1.0):
            raise ValueError(
                f"PrepaymentPrivileges: annual_lump_sum_pct must be a "
                f"fraction in [0, 1] (e.g. 0.10 = 10% of original "
                f"principal per year), got {self.annual_lump_sum_pct}."
            )
        if self.payment_increase_multiplier < 0:
            raise ValueError(
                f"PrepaymentPrivileges: payment_increase_multiplier cannot "
                f"be negative (a lender grants extra payment room, it never "
                f"charges for paying LESS), got "
                f"{self.payment_increase_multiplier}."
            )
        if self.penalty_months_interest < 0:
            raise ValueError(
                f"PrepaymentPrivileges: penalty_months_interest cannot be "
                f"negative (declare an open term instead of a fake zero "
                f"penalty window — DP#32: a declared zero is a value, a "
                f"negative one is nonsense), got "
                f"{self.penalty_months_interest}."
            )
        if self.standard_variable_rate < 0:
            raise ValueError(
                f"PrepaymentPrivileges: standard_variable_rate must be >= 0, "
                f"got {self.standard_variable_rate}. It is REQUIRED: the "
                f"penalty is computed at the lender's standard variable rate "
                f"in vigour, never derived from the borrower's discounted "
                f"contract rate."
            )

    @property
    def annual_lump_sum_allowance(self) -> float:
        """The penalty-free lump-sum room granted EACH calendar year."""
        return self.original_principal * self.annual_lump_sum_pct


# =============================================================================
# Pure functions — the free/penalized split and the penalty
# =============================================================================

def split_extra_payment(privileges: PrepaymentPrivileges,
                        requested_extra: float,
                        regular_payment: float,
                        lump_sum_used_ytd: float) -> dict:
    """Split ONE payment date's requested extra principal into free slices
    and the penalized overshoot.

    Order of consumption (the borrower exhausts the free room before paying
    a penalty on anything):

      1. the remaining annual lump-sum allowance
         (``annual_lump_sum_allowance - lump_sum_used_ytd``, floored at 0 —
         over-use earlier in the year leaves genuine negative room);
      2. the payment-increase allowance at THIS payment
         (``payment_increase_multiplier * regular_payment``);

    everything beyond both is the ``excess`` the penalty prices.

    The lump-sum bucket ALONE consumes annual allowance: the
    payment-increase privilege is a separate mechanism with no annual cap
    (repeatable at every payment), so riding it never reduces the year's
    10% room.

    Pure (DP#3): a function of the declared terms and the four numbers
    above — no schedule state, no hidden accumulator.

    Returns ``{'lump_sum': ..., 'payment_increase': ..., 'excess': ...}``.
    """
    if requested_extra < 0:
        raise ValueError(
            f"split_extra_payment: requested_extra cannot be negative "
            f"(a prepayment is money paid TO the lender), got "
            f"{requested_extra}."
        )
    remaining_allowance = max(
        0.0, privileges.annual_lump_sum_allowance - lump_sum_used_ytd)
    lump_sum = min(requested_extra, remaining_allowance)
    increase_room = max(0.0, privileges.payment_increase_multiplier
                        * regular_payment)
    after_lump = requested_extra - lump_sum
    payment_increase = min(after_lump, increase_room)
    excess = after_lump - payment_increase
    return {'lump_sum': lump_sum, 'payment_increase': payment_increase,
            'excess': excess}


def excess_penalty(privileges: PrepaymentPrivileges, excess: float) -> float:
    """The penalty on a penalized excess: ``penalty_months_interest``
    months of interest at the STANDARD VARIABLE rate in vigour.

    Not the borrower's contract rate — the signed convention computes the
    charge off the lender's posted standard variable rate, which is HIGHER
    than the discounted rate the household actually pays (using the lower
    one would flatter every aggressive-paydown strategy the optimizer
    ranks)."""
    return excess * privileges.standard_variable_rate * (
        privileges.penalty_months_interest / 12.0)


def price_monthly_extra(privileges: PrepaymentPrivileges,
                        applied_extra: float,
                        regular_payment: float,
                        lump_sum_used_ytd: float,
                        closes_loan: bool) -> dict:
    """The ONE entry point ``amortization_schedule`` calls each month.

    Prices the extra principal ACTUALLY APPLIED at one payment date (the
    caller has already capped it at the remaining balance):

      - normally, ``split_extra_payment`` decides the free vs penalized
        split and the penalty prices the overshoot;
      - when ``closes_loan`` is true — this payment repays the tranche IN
        FULL — neither privilege applies (clause 1 of the convention: the
        lump-sum privilege "does not apply when repaying the tranche in
        full"), so the ENTIRE applied extra is the excess and the penalty
        prices all of it.

    Returns ``{'free': ..., 'excess': ..., 'penalty': ...}`` where ``free``
    is the sum of both free slices.
    """
    if closes_loan:
        excess = max(0.0, applied_extra)
        return {'lump_sum': 0.0, 'payment_increase': 0.0, 'free': 0.0,
                'excess': excess,
                'penalty': excess_penalty(privileges, excess)}
    split = split_extra_payment(privileges, applied_extra, regular_payment,
                                lump_sum_used_ytd)
    return {'lump_sum': split['lump_sum'],
            'payment_increase': split['payment_increase'],
            'free': split['lump_sum'] + split['payment_increase'],
            'excess': split['excess'],
            'penalty': excess_penalty(privileges, split['excess'])}
