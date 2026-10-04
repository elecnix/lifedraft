"""What a dated ``[from, to)`` interval MEANS -- one module, one convention.

Issue #415. Four sites in this repo read the same declared fact -- a dated
income interval with an annual-rate ``amount`` -- and disagreed about what it
means:

  1. ``countries/canada/cpp_estimator.py`` -- the CPP overlay loop admitted a
     whole CALENDAR YEAR on any overlap, crediting ~198% of the declared annual
     rate for a part year, and (in the same function) answered the boundary
     instant two different ways: its activity test treats ``to <= as_of`` as
     inactive while its overlay loop still credits the year.
  2. ``contract_people._active_employment_income`` -- ``inc["to"] < as_of``,
     so an income ending EXACTLY on ``as_of`` stayed active and the fold grew
     that salary across the whole horizon. This scalar is not cosmetic: it is
     ``SimulationConfig._primary_income`` (``simulation.py:2216``) and therefore
     the base every downstream tax, RRSP-room and clawback figure grows from.
  3. ``simulation._income_components_for_year`` / ``_self_employment_income_for_year``
     -- day-count blending, which ``countries/canada/earned_income.py`` already
     documents as the more correct approach.
  4. ``countries/canada/pre_designation.period_years`` -- INCLUSIVE ``to``.

Nothing cross-checked them, so nothing failed. AGENTS.md records the trap
class: *"a date window that skips once turned a $250k salary into $0, and the
optimizer cheerfully ranked strategies against it."* With ~6,600 tests and no
shared module, these four could not cross-check each other even if a test had
tried -- the arithmetic was inlined four times, four different ways.

This module is the shared arithmetic. It is deliberately jurisdiction-neutral
(DP#25): half-open intervals and calendar-year day counts are not Canadian tax
law, so core imports nothing from ``countries/`` and every jurisdiction program
may import this.

THE CONVENTION, stated once
---------------------------

A dated window is **half-open: ``[from, to)``.** ``to`` is the first day NOT
covered. This is not an invention of this module -- it is what the input
contract already says. ``schema/defs/people.json`` $defs/income.amount reads
*"Annual gross amount in effect over [from, to)"*, and $defs/income.to reads
*"null = ongoing/no known end"*. The bracket notation is the contract's own.
``from <= on < to`` is therefore the only activity test that can be right, and
an income whose ``to`` equals the snapshot date is OVER on that date.

``to = None`` is the schema's EXPLICIT spelling of "no known end", resolved to
an unbounded upper limit here. That is not a fallback for missing input and
never coerces a supplied value (DP#32) -- the schema requires the key, so its
absence is a contract violation that fails validation before this module sees
it.

The PRIMITIVE is :func:`overlap_days_between` -- the count of DAYS where two
half-open intervals intersect. Everything else here is that one count, taken
over a different interval:

  * :func:`overlap_days`      -- intersect with a CALENDAR YEAR.
  * :func:`year_fraction`     -- that count over the year's length: the fold's
                                weighting, since ``amount`` is an annual RATE.
  * :func:`overlaps_year`     -- "does this window touch this year at all",
                                the admission test.
  * :func:`covers_date`       -- the boundary question, asked as a one-day
                                interval, so ``to == on`` is NOT covered.

Pure functions over explicit arguments (DP#3); no hidden state, no I/O.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Tuple

# The upper limit substituted for an open-ended (``to is None``) window. A
# window that runs "from 2018 onward" is bounded only by the horizon a caller
# actually asks about, and every caller here asks about a year or a day.
_FOREVER = date.max


def parse_date(iso: str) -> date:
    """The one ISO ``YYYY-MM-DD`` parse, with one error message.

    Consumers that previously each called ``date.fromisoformat`` inline (and
    each raised the same ``ValueError`` with the same unreadable text) now fail
    the same way. The value is returned, never defaulted: a malformed date is
    refused, never coerced (DP#32).
    """
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"expected an ISO YYYY-MM-DD date, got {iso!r}: {exc}"
        ) from exc


def _intersect(window_from: date, window_to: Optional[date],
               lo: date, hi: date) -> Tuple[date, date]:
    """The intersection of ``[window_from, window_to)`` with ``[lo, hi)``.

    The half-open convention lives HERE and nowhere else. An open-ended window
    contributes no upper bound of its own; the caller's ``hi`` supplies it.
    """
    start = window_from if window_from > lo else lo
    end = hi if window_to is None or window_to > hi else window_to
    return start, end


def overlap_days_between(window_from: date, window_to: Optional[date],
                         lo: date, hi: date) -> int:
    """THE primitive: days where ``[window_from, window_to)`` meets ``[lo, hi)``.

    ``0`` when the two intervals are disjoint or merely touch --
    ``[a, b)`` and ``[b, c)`` share no day, which is the whole content of the
    half-open convention and the thing readers 1, 2 and 3 of #415 each got
    differently. Negative results are clamped to ``0`` rather than returned, so
    every caller can compare the count directly without re-deriving direction.
    """
    start, end = _intersect(window_from, window_to, lo, hi)
    days = (end - start).days
    return days if days > 0 else 0


def year_bounds(calendar_year: int) -> Tuple[date, date]:
    """``[Jan 1, next Jan 1)`` for a calendar year -- the year as an interval."""
    return date(calendar_year, 1, 1), date(calendar_year + 1, 1, 1)


def overlap_days(window_from: date, window_to: Optional[date],
                 calendar_year: int) -> int:
    """Days of the window that fall inside ``calendar_year`` (``0`` if none)."""
    return overlap_days_between(window_from, window_to, *year_bounds(calendar_year))


def overlaps_year(window_from: date, window_to: Optional[date],
                  calendar_year: int) -> bool:
    """Does the window touch this calendar year at all?

    An admission test, for programs whose unit is the whole calendar year. It
    deliberately answers only "does it touch", never "how much" -- how much is
    :func:`year_fraction`'s job, and conflating the two is what let the CPP
    overlay credit a full annual rate for six months of work.
    """
    return overlap_days(window_from, window_to, calendar_year) > 0


def year_fraction(window_from: date, window_to: Optional[date],
                  calendar_year: int) -> float:
    """The window's share of ``calendar_year``, in ``[0, 1]``.

    The weighting for a declared ``amount`` that the schema defines as an
    ANNUAL RATE: the year's income is the rate times the fraction of the year
    the window covers. ``countries/canada/earned_income.py`` names this as the
    more correct approach, and :func:`simulation._income_components_for_year`
    has always done it -- this is that arithmetic, spelled once.
    """
    year_start, year_end = year_bounds(calendar_year)
    return overlap_days_between(window_from, window_to, year_start, year_end) / \
        (year_end - year_start).days


def covers_date(window_from: date, window_to: Optional[date], on: date) -> bool:
    """Is ``on`` inside the window? Half-open: ``from <= on < to``.

    Asked as a one-day interval so it is the SAME primitive rather than a
    fourth restatement of the comparison. The consequence worth stating out
    loud, because reader 2 of #415 got it backwards: an income whose ``to`` IS
    ``on`` does **not** cover ``on``. It ended on that date.
    """
    return overlap_days_between(window_from, window_to, on, on + timedelta(days=1)) > 0


def resolve_window(window_from: str, window_to: Optional[str]) -> Tuple[date, Optional[date]]:
    """Parse a stored ``{"from": ..., "to": ...}`` pair into window bounds.

    For consumers that hold the contract's ISO strings rather than parsed
    dates. Returns ``(from, to)`` with ``to`` left ``None`` when open-ended, so
    the open-ended case stays visible to the reader instead of being laundered
    into a sentinel date by the caller.
    """
    return parse_date(window_from), None if window_to is None else parse_date(window_to)