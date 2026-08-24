"""The superficial-loss rule (ITA s.54 "superficial loss", s.53(1)(c)) — issue #141.

A realized capital LOSS is DENIED outright -- never deductible, never
joinable to the s.111(1)(b) carry-forward pool (#140) -- when ALL of:

1. the taxpayer disposes of property and realizes a loss;
2. within the window of 30 calendar days BEFORE or AFTER the disposition,
   the taxpayer OR AN AFFILIATED PERSON (a spouse, or a controlled
   corporation/partnership/trust) acquires the same or an identical
   property (a "substituted property");
3. the acquiring person still owns the substituted property 30 calendar
   days after the disposition.

The denial does not destroy the benefit -- it DEFERS it (s.53(1)(f)): the
denied loss is ADDED TO THE ACB of the substituted property, so it resurfaces
as a smaller gain (or larger loss) when THAT property is eventually disposed
of. (For borrowed-money purchases s.53(1)(g.1) routes the denied loss into
the traced borrowing balance instead; composing that with the engine's
s.20(1)(c) purpose-tracing machinery is out of scope here.)

REPRESENTATION HONESTY (the issue's own difficulty statement): the statute is
a DAY-granular 60-day window over identified securities; this engine holds
the non-registered sleeve as ONE aggregated pot with a single ACB and no
per-lot acquisition ledger, and its simulation step is a YEAR. Auto-detecting
the window from aggregated balances is therefore impossible without building
a per-lot transaction ledger -- which nothing else in the engine needs yet.
So the rule ships DECLARABLE: the household declares each disposition's facts
(``people[].superficial_losses[]`` -- the calendar year, the pre-inclusion
loss amount, who acquired the substituted property, how many days separated
the acquisition from the disposition, and whether it was still held 30 days
after), and THIS module applies the statute to those facts exactly --
including the threshold sides (|days| > 30 or sold-out-before-day-30 means
NOT superficial, the loss is fully allowed). The step-scale abstraction and
its declarable-only detection are disclosed on every run that activates the
feature (model_fidelity, issue #141 entry).

Pure functions only (DP#3): no hidden state, no I/O, deterministic.
"""

from __future__ import annotations

#: ITA s.54(b)(i)/(c)(i): the substitution window is 30 calendar days on
#: EACH side of the disposition.
WINDOW_DAYS = 30


def acquisition_in_window(days_to_acquisition: int) -> bool:
    """Whether an acquisition ``days_to_acquisition`` days from the
    disposition falls inside the s.54 window ([-30, +30], endpoints included).

    Signed: NEGATIVE = the substituted property was acquired BEFORE the
    disposition, POSITIVE = after. Both sides count (s.54(b)(i)).
    """
    return abs(days_to_acquisition) <= WINDOW_DAYS


def is_superficial(days_to_acquisition: int,
                   still_held_30_days_after: bool) -> bool:
    """The s.53(1)(c)/s.54 test: is this disposition's loss DENIED?

    True iff the substituted property was acquired inside the 30-day window
    AND was still owned 30 days after the disposition. Either condition
    failing means the loss is fully ALLOWABLE (both sides of every threshold
    are real, reachable outcomes -- DP#17).
    """
    return acquisition_in_window(days_to_acquisition) \
        and bool(still_held_30_days_after)


def denied_loss(loss_amount: float, days_to_acquisition: int,
                still_held_30_days_after: bool) -> float:
    """The DENIED portion of a disposition's pre-inclusion capital loss:
    the whole loss when the disposition is superficial, otherwise 0.0.

    Raises:
        ValueError: on a negative ``loss_amount`` (DP#32: a negative loss
            magnitude is not a fact this module can interpret -- refuse
            loudly, never silently flip its sign into a gain).
    """
    if loss_amount < 0.0:
        raise ValueError(
            f"a capital-loss magnitude cannot be negative; got "
            f"{loss_amount!r} -- refuse loudly rather than silently treat "
            f"it as a gain (DP#32)")
    if not is_superficial(days_to_acquisition, still_held_30_days_after):
        return 0.0
    return float(loss_amount)
