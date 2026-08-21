"""Principal-residence exemption (PRE), per property, per year — ITA s.40(2)(b).

Epic #690 bite 4 / issue #695. The exemption fully shelters the capital gain on
disposition of a property, but it is **one property per family unit per calendar
year**: a family that owns a home *and* a cottage cannot exempt both for the same
year. Designating the cottage for a set of years means the **home** loses the
exemption for those same years and pays capital-gains tax on the gain apportioned
to them at disposition (deemed, at death, in this model). Which property to
designate, and for which years, is a genuine dollar-valued choice — the same kind
of election the tool already compares elsewhere (RRSP deduct-now-vs-later, CCA
claim-or-not) — not a fixed fact.

This module is the pure tax arithmetic (DP#10/#25: the Canada program area owns
it; core imports nothing from here). It does three things and nothing else:

  1. ``designated_years`` — the set of calendar years a property is designated,
     from its ``designated_principal_residence_years`` periods (a ``to`` of
     ``None`` means "still designated as of the document's as_of").
  2. ``taxable_gain_fraction`` — the ITA s.40(2)(b) apportionment. The exempt
     fraction of a property's gain is ``(1 + designated_years) / years_owned``
     (the statute's "+1" convenience year), capped at 1; the taxable fraction is
     the remainder. A property designated for **zero** years gets the "+1" only
     if it is designated at all, so zero designated years ⇒ the whole gain is
     taxable (no bonus for a property you never designate).
  3. ``family_year_conflict`` — the "one property per family unit per year"
     rule, returned as data (the first offending ``(year, id_a, id_b)``) so the
     loader can reject it loudly rather than silently resolving a document that
     double-claims a year (DP#32: two properties designating the same family-year
     is invalid input, not a pick-one).

``years_owned`` is not a fact the contract collects (a property carries a value
and an ACB, not an acquisition date), so this model uses the **family's
designation window** — the span from the earliest designated year to the latest,
across all the family's properties — as the shared ownership horizon. Two
co-owned properties are held over the same window, so the denominator is the
same for both; the numerator (each property's designated-year count) is what the
allocation moves. This is a declared-data-only approximation of the statute's
per-property ``years_owned`` and is stated as such (it is exact when both
properties are held for the whole window, the common case).
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple


def _year(iso_date: str) -> int:
    """The calendar year of an ISO ``YYYY-MM-DD`` date."""
    return int(iso_date[:4])


def period_years(period: Dict, as_of_year: int) -> Set[int]:
    """The inclusive set of calendar years one designation period covers.

    ``to = None`` means the designation is still in effect as of the document's
    ``as_of`` (not yet closed out), so it runs through ``as_of_year``.
    """
    start = _year(period["from"])
    end = as_of_year if period["to"] is None else _year(period["to"])
    if end < start:
        raise ValueError(
            f"designation period ends ({end}) before it begins ({start}): "
            f"{period!r}")
    return set(range(start, end + 1))


def designated_years(periods: Iterable[Dict], as_of_year: int) -> Set[int]:
    """Union of the calendar years covered by a property's designation periods."""
    years: Set[int] = set()
    for period in periods:
        years |= period_years(period, as_of_year)
    return years


def family_window_years(years_by_property: Dict[str, Set[int]]) -> int:
    """The family's shared ownership/designation horizon, in whole years.

    The span from the earliest designated year to the latest across ALL the
    family's properties (inclusive). 0 when nothing is designated anywhere.
    """
    all_years: Set[int] = set()
    for years in years_by_property.values():
        all_years |= years
    if not all_years:
        return 0
    return max(all_years) - min(all_years) + 1


def taxable_gain_fraction(designated_count: int, window_years: int) -> float:
    """Fraction of a property's capital gain that is TAXABLE (ITA s.40(2)(b)).

    Exempt fraction = min(1, (1 + designated_count) / window_years); taxable is
    the remainder. A property with zero designated years is fully taxable (the
    "+1" bonus year applies only to a property actually designated).
    """
    if designated_count <= 0:
        return 1.0
    if window_years <= 0:
        raise ValueError(
            "window_years must be positive when a property has designated years")
    exempt = min(1.0, (1 + designated_count) / window_years)
    return max(0.0, 1.0 - exempt)


def family_year_conflict(
        years_by_property: Dict[str, Set[int]]
) -> Optional[Tuple[int, str, str]]:
    """First ``(year, property_a, property_b)`` two properties both designate.

    ``None`` when the allocation is valid (no family-year claimed twice). Returned
    as data, not raised, so the caller owns the error type it presents.
    """
    claimed: Dict[int, str] = {}
    for pid in sorted(years_by_property):
        for year in sorted(years_by_property[pid]):
            if year in claimed:
                return (year, claimed[year], pid)
            claimed[year] = pid
    return None
