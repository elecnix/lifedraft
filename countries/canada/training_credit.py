#!/usr/bin/env python3
"""The Canada Training Credit (ITA s.122.91, issue #372): a REFUNDABLE federal
credit for the tuition an adult learner pays, limited by a personal, per-person
"Canada training amount limit" that builds $250 at a time.

The engine had no model of this at all, which makes an adult learner's return
wrong in two directions at once: they get no refundable credit, and the
non-refundable s.118.5 tuition credit is computed on the FULL declared tuition
because nothing has reduced the base by the CTC first (ITA s.122.91(3) requires
exactly that reduction -- CRA, "Canada training credit": "Eligible tuition fees
for the Tuition Tax Credit will be reduced by the amount of the training tax
credit deducted").

How the limit builds (the part that is easy to get subtly wrong)
--------------------------------------------------------------
``$250`` is added to the limit for a year only when EVERY condition held **in
the PRECEDING year**:

- the individual was aged 25 or older and under 65 at the END of the preceding
  year;
- their WORKING income in the preceding year was at least CRA's indexed
  threshold ($10,000 in 2020, $12,058 in 2026);
- their NET income in the preceding year did not exceed the top of the third
  federal bracket.

The preceding-year test is not a detail. Reading CRA's own worked example for
the 2026 taxation year: "to accumulate the annual training amount limit of
$250 for the 2026 taxation year ... have total working income of $12,058 or
more in 2025 ... have individual net income for 2025 that did not exceed
$177,882" (CRA, Canada training credit; thresholds per the CRA's published
table). The issue's shorthand -- "$250 in each year the person is 25 to 64 at
year end, has working income of at least the indexed threshold" -- reads as a
same-year test, and modelling it that way would let a member who earned nothing
this year bank $250 anyway. The caller passes the PRECEDING year's figures and
this module says yes or no; the fold owns carrying them.

What is in this module and what is not
--------------------------------------
The AGES and the 50% rate are fixed by the statute and are named constants here
with their citations -- they have never been indexed and never will be, so a
year-versioned table row would be a second place for them to be wrong. The
MONEY amounts ($250, $5,000, and the indexed working-income threshold) live in
the year-versioned ``TaxYearData`` (``ctc_annual_accrual``, ``ctc_lifetime_cap``,
``ctc_working_income_threshold``), because the threshold genuinely moves each
year (DP#2/DP#12/DP#20).

Every function is pure (DP#3) and reads nothing but its arguments (DP#25): the
fold does the plumbing, this module holds the law.

References:
    ITA s.122.91 -- https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-122.91.html
    CRA, "Canada training credit" -- https://www.canada.ca/en/revenue-agency/services/child-family-benefits/canada-training-credit.html
    CRA Form 5000-S11 Schedule 11 (Federal Tuition Amount and Canada Training Credit)
"""

from __future__ import annotations

# ITA s.122.91(3): the accrual window, measured at the END of the year whose
# income is being tested. CRA's wording, quoted so the arithmetic below cannot
# be read the other way round: a taxpayer accrues when they are "aged 25 to
# under 65 at December 31". "UNDER 65" is what makes the maximum 64 and the
# comparison inclusive: a member who turns 65 during the year is 65 at December
# 31, is therefore OUT of the window, and earns nothing in it. (Three separate
# review rounds read this as 65 being inside the window; the quoted sentence is
# here so the next one does not have to re-derive it.)
ACCRUAL_MIN_AGE = 25
ACCRUAL_MAX_AGE = 64  # inclusive: 25..64, because the statute says "under 65"

# ITA s.122.91(1): the credit may be claimed by an individual aged 26 or older.
# The window's far end is the age at which the limit EXPIRES (see
# ``room_after_claim``), not a claim-age bar the Act draws separately.
CLAIM_MIN_AGE = 26

# ITA s.122.91(1)(a): the claim is 50% of the eligible tuition and fees.
CREDIT_RATE = 0.5


def accrual_for_year(age_at_year_end: int, working_income: float,
                     net_income: float, annual_accrual: float,
                     working_income_threshold: float,
                     third_bracket_ceiling: float) -> float:
    """The ``$250`` added to this individual's training amount limit for the
    year that FOLLOWS the year whose facts are passed in.

    Args:
        age_at_year_end: the individual's age at the end of the year whose
            income is being tested (25..64 accrues).
        working_income: their WORKING income (employment income and
            commission income) in that year.
        net_income: their net income for that year.
        annual_accrual: CRA's annual accrual for the year (``$250``; year-
            versioned data, not a constant here).
        working_income_threshold: CRA's INDEXED working-income threshold for
            that year.
        third_bracket_ceiling: the top of the THIRD federal bracket for that
            year -- the net-income ceiling. Passed in rather than looked up so
            this module has one input per condition and no data access (DP#3);
            the caller reads it off the same bracket table every other rate uses
            (DP#9), so there is no second copy of the brackets to drift.

    Returns:
        ``annual_accrual`` when every condition held, else ``0.0``.

    A missing figure is not guessed at: ``annual_accrual <= 0`` (no data for
    the year) yields ``0.0``, because a credit whose parameters are unknown must
    not be invented (DP#32). The caller surfaces that as "no credit" rather
    than as a silent zero-amount claim on a real return.
    """
    if annual_accrual <= 0.0:
        return 0.0
    # A missing ceiling must not MANUFACTURE a credit. `third_bracket_ceiling`
    # returns 0.0 when the bracket table is too short to have a third bracket,
    # and the income test below is `net_income > ceiling` -- so with a ceiling of
    # 0.0 a member reporting exactly $0 of net income would satisfy it and bank
    # $250 off a table we could not read. Refusing outright when the threshold
    # or the ceiling is non-positive closes that: no data, no accrual. The same
    # reasoning applies to the working-income threshold (0.0 would admit anyone).
    if working_income_threshold <= 0.0 or third_bracket_ceiling <= 0.0:
        return 0.0
    if not (ACCRUAL_MIN_AGE <= age_at_year_end <= ACCRUAL_MAX_AGE):
        return 0.0
    if working_income < working_income_threshold:
        return 0.0
    if net_income > third_bracket_ceiling:
        return 0.0
    return annual_accrual


def credit_for_year(opening_room: float, eligible_tuition: float,
                    credit_rate: float = CREDIT_RATE,
                    lifetime_cap: float = 0.0,
                    claimed_to_date: float = 0.0) -> float:
    """The CTC claimed for one taxation year (ITA s.122.91(1)).

    The claim is the LEAST of three things:

    1. the training amount limit carried into the year (the unused balance from
       prior years, which is what makes a learner's second and third course
       cheaper to study than their first);
    2. ``credit_rate`` of the eligible tuition and fees paid in the year
       (``50%``, ITA s.122.91(1)(a));
    3. what is left of the ``$5,000`` lifetime limit -- ``lifetime_cap``
       minus everything already claimed.

    All three prongs are needed. Omitting (3) is the one that turns a lifetime
    cap into a suggestion: a long-lived learner with forty years of unused
    room could otherwise claim the full 50% every year forever. The caller keeps
    the ROOM itself inside the lifetime cap (see ``room_after_claim``), so
    (3) is a belt-and-braces second check rather than the only one -- but a
    household whose opening room was declared from a notice of assessment
    arrives here with a figure this module must still be able to bound.

    Returns ``0.0`` when there is no room, no tuition, or no parameters -- a
    genuine zero, not a missing input (DP#32: the difference is stated in the
    caller's disclosure, never papered over). Every input the caller reads off
    year-versioned data: ``annual_accrual`` and ``working_income_threshold`` are
    CRA's published figures and ``third_bracket_ceiling`` is the top of the
    third bracket, all read from the same federal row every other rate uses.
    """
    if opening_room <= 0.0 or eligible_tuition <= 0.0:
        return 0.0
    claim = min(opening_room, eligible_tuition * credit_rate)
    if lifetime_cap > 0.0:
        lifetime_room = lifetime_cap - claimed_to_date
        if lifetime_room <= 0.0:
            return 0.0
        claim = min(claim, lifetime_room)
    return max(0.0, claim)


def room_after_claim(opening_room: float, claim: float, annual_accrual: float,
                     lifetime_cap: float) -> float:
    """The training amount limit carried into the NEXT year.

    ``opening_room + accrual - claim``, then clamped so the limit can never
    exceed what is left of the ``$5,000`` lifetime cap. Clamping HERE (rather
    than only inside ``credit_for_year``) is what keeps the lifetime cap a real
    ceiling on the balance: without it, a member who never studies would
    accumulate $250/year past $5,000 and the cap would only bite on the claim,
    after the fact.

    A negative result is floored at 0.0 -- a limit is not a liability.
    """
    room = opening_room + max(0.0, annual_accrual) - max(0.0, claim)
    if lifetime_cap > 0.0:
        room = min(room, lifetime_cap)
    return max(0.0, room)


def claimable_at(age_at_year_end: int, opening_room: float,
                 eligible_tuition: float,
                 credit_rate: float = CREDIT_RATE,
                 lifetime_cap: float = 0.0,
                 claimed_to_date: float = 0.0) -> float:
    """The full claim for a year, age-gated.

    The age gate is applied HERE rather than left to each caller: a member who
    is not yet 26 has no limit to draw on in the first place (their accruals
    only start at 25 and only land in the following year), so the room is
    ``0.0`` and the claim is ``0.0`` by arithmetic. Stating the gate makes that
    consequence explicit rather than incidental.
    """
    if age_at_year_end < CLAIM_MIN_AGE:
        return 0.0
    return credit_for_year(opening_room, eligible_tuition, credit_rate,
                           lifetime_cap, claimed_to_date)


def tuition_after_credit(eligible_tuition: float, claim: float) -> float:
    """The tuition amount left for the s.118.5 non-refundable credit once the
    CTC has been claimed against it (ITA s.122.91(3)).

    CRA is explicit: "Eligible tuition fees for the Tuition Tax Credit will be
    reduced by the amount of the training tax credit deducted." The claim can
    never exceed 50% of the tuition, so this cannot go negative -- and the
    ``max(0.0, ...)`` is there so that a caller which computed the claim from a
    DIFFERENT (larger) room cannot turn the tuition base into a negative
    deduction, which would read downstream as a tuition credit INCREASE.
    """
    return max(0.0, eligible_tuition - max(0.0, claim))


def third_bracket_ceiling(federal_brackets) -> float:
    """The top of the THIRD federal bracket -- the CTC's net-income ceiling.

    Read off the bracket table the engine already uses for every other rate
    (DP#9: one table, one answer), rather than stored as a parallel "CTC
    income limit" that could disagree with the brackets it is derived from.
    Returns ``0.0`` for a table too short to have a third bracket -- which
    makes ``net_income > 0`` true for any positive income, i.e. no accrual.
    That is the conservative direction: a missing ceiling does not manufacture
    a credit.
    """
    if not federal_brackets or len(federal_brackets) < 3:
        return 0.0
    top = federal_brackets[2].max_income
    return float(top) if top else 0.0
