#!/usr/bin/env python3
"""Runway — how many months a household survives after an income shock
(issue #758).

## The question

*"How much runway do I have if I lose my job now, or in a year?"*

The engine could already rank terminal net worth under a permanent income
change, and (since #679) detect that a household *became insolvent at some
point*. It could not say **"you have N months."** That is the number a
household wants before signing a mortgage, and it is the number that decides
whether a bad year forces the sale of the house. This module produces it.

## What runway means, precisely (and the two honest endpoints)

Given an income shock beginning at date ``D``, runway is the elapsed time
from ``D`` until the household **first cannot fund its committed outflows
from income plus liquid resources**. The solvency model (#679, the
``'solvency'`` rule in ``simulation_rules.apply_solvency``) already computes
the cash-flow identity every year and runs the liquidation waterfall when it
fails. This module COMPOSES that existing per-year verdict into a single
months figure — it does not re-simulate and does not re-invent the identity
(DP#9: one spelling of the rule).

There are two distinct endpoints, and both are reported:

* **Ruin** — the first year the waterfall exhausts EVERY liquid source and
  the household is STILL short (``YearResult.ruined``). This is the precise
  moment the household "cannot fund from income plus liquid resources" — the
  definition the issue states first. ``runway_months`` (the headline) is
  months to ruin.
* **Stress begins** — the first year the waterfall engages at all
  (``solvency_shortfall > 0``), i.e. the household starts eating its liquid
  cushion but has not yet run out. The issue's parenthetical ("the first
  ``solvency_shortfall`` year") names this point. It is reported as
  ``stress_begins_months`` because the two are NOT the same number, and
  conflating them is exactly the "confident wrong number" this repo exists
  to kill.

## The months question — option (b), a LABELLED interpolation, with an honest bracket

The engine steps in YEARS. Runway is a MONTHS question, and the difference
between 7 and 11 months is decision-relevant. The issue offers three
options; this module chooses **(b): an explicit interpolation that states it
is an interpolation**, always co-reported with **(c): an honest bracket**.

The bracket is structural and needs no approximation: the household is
solvent through the end of the last non-ruined year, and ruined during the
ruin year, so runway is *between* ``months_to_start_of_ruin_year`` and
``months_to_end_of_ruin_year`` — never outside it.

The interpolation point inside that bracket uses the waterfall's OWN
measurement of how far the liquid stock stretched in the ruin year: in the
ruin year the household needed ``solvency_shortfall`` (gross annual gap)
and the waterfall delivered ``solvency_covered`` before exhausting. Under a
uniform-monthly-burn assumption, the liquid stock lasted a fraction
``covered / shortfall`` of the ruin year, so

    runway_months ≈ months_to_start_of_ruin_year + (covered/shortfall) × 12

This is labelled ``interpolated=True`` and ``method="linear interpolation
within the ruin year (uniform monthly burn)"``. The uniform-burn assumption
is an approximation — the shock year's income is already day-count-blended
by #674's ``_income_components_for_year``, so the true exhaustion month is
*within* the bracket but not necessarily at the linear point — and the
bracket is always shown beside the point so a reader is never asked to trust
the point alone (DP#32: an estimate must be labelled as one).

## What is NOT modelled here (and is labelled, not hidden)

Runway reuses the simulated solvency verdict as-is. That verdict, by
construction of the ``'solvency'`` rule, counts the year's booked
contributions as a required outflow and treats ALL of
``household_budget.annual_living_costs`` as rigid. Both UNDERSTATE runway
(the household would in reality stop saving and compress discretionary
spending under a shock), and both are documented as caveats via
``model_fidelity.Approximation`` rather than silently absorbed:

* **Discretionary spending does not compress.** The contract's
  ``household_budget.annual_living_costs`` is a single scalar; there is no
  discretionary/non-discretionary split, so runway treats every dollar as
  non-discretionary. That UNDERSTATES runway. See the
  ``runway_treats_all_spend_as_rigid`` approximation.
* **Contributions are counted as committed.** The ``'solvency'`` rule
  includes the year's booked RRSP/TFSA/non-reg contributions in the required
  side of the identity. A household in real distress stops contributing; the
  engine keeps booking them (clamped to room, not affordability) and the
  waterfall then liquidates to cover. That double-handling adds liquidation
  tax friction and UNDERSTATES runway. See the
  ``runway_counts_contributions_as_committed`` approximation.
* **An unsecured credit line can be cut.** A runway figure that leans on a
  declared revolving credit facility assumes the lender does not reduce or
  cancel it — a lender can do exactly that when the borrower most needs it.
  See ``runway_relies_on_uncollateralized_credit``.

A runway that ERRS SHORT is the safe direction. The issue warns the other
way — a number that flatters — and reusing the engine's own (pessimistic)
solvency verdict is what keeps this on the safe side.

## Liquidation order — reported, not re-ordered

The #679 waterfall draws ``emergency_reserve -> revolving_credit -> non_reg
-> tfsa -> registered``. The issue suggests ``cash -> TFSA -> non-reg ->
credit -> RRSP``. They differ (the engine liquidates non-reg before TFSA;
the issue would liquidate TFSA first). This module does NOT re-order the
waterfall — that is #679's shared mechanism, reordering it would move the
golden invariant and is out of scope. The runway number reflects the
engine's ACTUAL liquidation, and the order difference is surfaced as a
finding (``runway_waterfall_order``).

The counter-intuitive RRSP point the issue makes — an RRSP withdrawal in a
job-loss year is taxed at an unusually LOW marginal rate, because that
year's income is low — the engine ALREADY gets right:
``ordinary_income_cost(ctx.primary_marginal_rate)`` prices the registered
draw at whatever this year's (low, job-loss-year) marginal rate is, not a
chosen one. That is reported as a POSITIVE finding, not a defect.

## DP status

DP#3 (pure function, no state), DP#9 (composes #679's solvency fold and
#674's dated income segments — one spelling of each rule), DP#25 (this is
a reporting-layer fold; it imports the INNER ``liquidation_waterfall`` /
``summarize_solvency``, never the simulation engine), DP#32 (absence of
``annual_living_costs`` fails loudly via ``engaged=False``; the months
estimate is labelled as an interpolation, never presented as exact).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from liquidation_waterfall import summarize_solvency

# A solar-year average month length, used ONLY to convert a day count into a
# months figure for the bracket and the interpolation. This is a unit
# conversion, not a financial assumption — no rate or bracket is hardcoded
# here (DP#2).
_DAYS_PER_MONTH = 365.25 / 12.0


def _months_between(start: date, end: date) -> float:
    """Elapsed months from ``start`` to ``end`` as a (possibly fractional)
    day count divided by the average month length.

    A signed quantity: negative when ``end`` precedes ``start`` (the caller
    treats a negative runway as "the shock and the ruin are in the same
    year, and ruin precedes the shock mid-window" — clamped to 0 upstream).
    Uses day arithmetic, not integer month truncation, so a 7-month vs
    11-month distinction is not rounded away (the issue's whole motivation).
    """
    return (end - start).days / _DAYS_PER_MONTH


def _end_of_year(calendar_year: int) -> date:
    """December 31 of ``calendar_year`` — the last day the household is
    still *within* that simulation year."""
    return date(calendar_year, 12, 31)


def _start_of_year(calendar_year: int) -> date:
    """January 1 of ``calendar_year``."""
    return date(calendar_year, 1, 1)


@dataclass(frozen=True)
class RunwayResult:
    """The months-to-insolvency metric for ONE simulated trajectory under
    ONE dated income shock (issue #758).

    A trajectory that NEVER ruins within the simulated horizon reports
    ``runway_months = None`` (the household survives the shock window the
    engine modelled) and ``survives_horizon_months`` as the floor — "at
    least this long." Neither is a finding of safety beyond the horizon; the
    engine simply did not simulate further.

    ``engaged`` mirrors ``summarize_solvency``'s flag: False when
    ``household_budget.annual_living_costs`` was never supplied, in which
    case every other field is the honest absence and the caller MUST refuse
    to print a number (DP#32 — printing "0 months" for an un-engaged run is
    the most dangerous thing this metric could say).
    """
    engaged: bool
    # Headline: months from the shock date to first RUIN. None when the
    # household never ruins within the horizon (survives). Always a labelled
    # interpolation when not None -- see ``interpolated`` / ``method``.
    runway_months: Optional[float] = None
    # The honest bracket the interpolation lives inside, as (lower, upper).
    # Both measured in months from the shock date. (None, None) when not
    # engaged or when the household survives the horizon.
    runway_months_bracket: Tuple[Optional[float], Optional[float]] = (None, None)
    # Months from the shock date to the first year the waterfall ENGAGES
    # (stress begins, liquid resources start being spent). None when the
    # household never falls short. This is NOT the same as runway_months.
    stress_begins_months: Optional[float] = None
    # "≥ this many months" floor when the household survives the horizon —
    # months from the shock date to the end of the last simulated year.
    survives_horizon_months: Optional[float] = None
    # Provenance of the point estimate, for the report. "n/a" when there is
    # no point (not engaged, or survives).
    interpolated: bool = False
    method: str = "n/a"
    # 0-based projection-year index of the first ruin / first shortfall, for
    # callers that want the engine's own year axis (not months).
    first_ruin_year: Optional[int] = None
    first_shortfall_year: Optional[int] = None
    # Caveats the caller must surface (also registered as Approximations,
    # but carried here so a pure-data consumer sees them without the
    # model_fidelity registry).
    relies_on_credit_facility: bool = False
    drew_registered: bool = False
    # The missing input, named, when ``engaged`` is False — never a bare
    # False the caller has to guess the reason for.
    unengaged_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        """Serialize for the JSON / console / HTML reports (DP#24).

        The bracket is a tuple (not JSON-native); serialized as a list.
        ``runway_months=None`` serializes as null -- the household survives
        the simulated horizon, NOT a finding of zero runway (the caller
        branches on ``engaged`` and on None, never on truthiness).
        """
        lower, upper = self.runway_months_bracket
        return {
            'engaged': self.engaged,
            'runway_months': self.runway_months,
            'runway_months_bracket': [lower, upper] if self.engaged else None,
            'stress_begins_months': self.stress_begins_months,
            'survives_horizon_months': self.survives_horizon_months,
            'interpolated': self.interpolated,
            'method': self.method,
            'first_ruin_year': self.first_ruin_year,
            'first_shortfall_year': self.first_shortfall_year,
            'relies_on_credit_facility': self.relies_on_credit_facility,
            'drew_registered': self.drew_registered,
            'unengaged_reason': self.unengaged_reason,
        }


def compute_runway(results: Sequence,
                    shock_date: Optional[date] = None,
                    start_year: Optional[int] = None) -> RunwayResult:
    """Fold a simulated trajectory into a months-to-ruin metric (issue #758).

    Pure (DP#3): a function of the ``YearResult`` list (duck-typed — this
    module does not import ``simulation_config``, DP#25) plus the shock's
    calendar date and the projection's start calendar year. It composes
    ``liquidation_waterfall.summarize_solvency`` for the engaged/ruin
    verdict and reads the per-year ``solvency_shortfall`` /
    ``solvency_covered`` / ``forced_liquidation_events`` the ``'solvency'``
    rule already stamped — it does not recompute the identity (DP#9).

    Args:
        results: the trajectory (list of ``YearResult``), as returned by
            ``FamilySimulation.run``. ``results[0]`` is the first projection
            year (calendar ``start_year``); ``YearResult.year`` is the
            0-based projection index.
        shock_date: the calendar date the income shock begins (the ``from``
            of the shock's first ``income_segments`` entry, #674). The
            runway is measured FROM this date, not from the projection
            start, so a swept shock date (now vs in 12 months) subtracts
            the pre-shock months honestly. When None, runway is measured
            from the start of the projection (``start_year``) — the
            "shock hits on day one" case, which is the issue's primary
            question.
        start_year: the calendar year of ``results[0]``. When None, read
            off the trajectory if available, else assumed to coincide with
            ``shock_date.year`` (the day-one-shock case).

    Returns a :class:`RunwayResult`. Never raises on a solvent or un-engaged
    trajectory — both are DATA (DP#32: an un-engaged run is reported as
    un-engaged, not as "0 months of runway").
    """
    if not results:
        return RunwayResult(engaged=False, unengaged_reason="empty trajectory")

    solvency = summarize_solvency(results)
    if not solvency['engaged']:
        return RunwayResult(
            engaged=False,
            unengaged_reason=(
                "household_budget.annual_living_costs is absent -- the #679 "
                "cash-flow identity never ran, so runway cannot be measured. "
                "Declare the household's measured annual working-phase spend "
                "to engage the check (DP#16/DP#32)."
            ),
        )

    # Resolve the calendar frame. YearResult.year is the 1-based projection
    # offset the engine stamps (year 1 == start_year, the first projection
    # year; year N == start_year + N - 1). When the caller did not state
    # start_year, fall back to the shock date's year (the day-one-shock case)
    # so the bracket still has a calendar anchor.
    if start_year is None:
        if shock_date is not None:
            start_year = shock_date.year
        else:
            # No shock date and no start year: anchor the frame on the
            # trajectory's own first index as calendar year 0-relative. The
            # month math then measures months from "the start of year 1",
            # i.e. runway is in projection-months from results[0]. This is
            # the day-one-shock case with no calendar anchor stated.
            start_year = 1
    shock_anchor = shock_date if shock_date is not None else _start_of_year(start_year)

    # ``first_ruin_year`` / ``first_shortfall_year`` are computed HERE over
    # WORKING-LIFE years only, not taken from summarize_solvency. Reason
    # (issue #758): runway is a WORKING-LIFE metric -- time from an income
    # shock until committed outflows can no longer be met during working
    # life. A solvency shortfall in RETIREMENT (e.g. a mortgage not fully
    # paid off by retirement, which the #679 identity now correctly prices
    # as `debt_service` only) is a retirement-plan question (#707's
    # decumulation domain), NOT a shock-induced runway event. Counting it
    # would report a $250k-earner as having "stress begins in year 10"
    # merely because they retire in year 10 -- the exact conflation this
    # fix removes. ``any_retired`` (YearResult, stamped by simulate_year_pure)
    # is the precise retirement signal.
    working = [r for r in results if not getattr(r, 'any_retired', False)]
    # `engaged` from summarize_solvency is across all years; for runway we
    # only care that the solvency rule ran at all (living_costs declared).
    ruin_year_idx: Optional[int] = None
    shortfall_year_idx: Optional[int] = None
    for r in working:
        if (shortfall_year_idx is None
                and getattr(r, 'solvency_shortfall', 0.0) > 0):
            shortfall_year_idx = getattr(r, 'year', None)
        if (ruin_year_idx is None and getattr(r, 'ruined', False)):
            ruin_year_idx = getattr(r, 'year', None)
        if ruin_year_idx is not None and shortfall_year_idx is not None:
            break

    # ── Caveats carried from the WORKING-LIFE forced-liquidation events ─
    # (Retirement-year forced sales are the drawdown / mortgage-gap, not a
    # runway credit-line reliance -- exclude them from the runway caveats.)
    by_source: Dict[str, float] = {}
    for r in working:
        for ev in getattr(r, 'forced_liquidation_events', ()):
            src = ev.get('source', '?')
            by_source[src] = by_source.get(src, 0.0) + ev.get('gross_drawn', 0.0)
    relies_on_credit = by_source.get('revolving_credit', 0.0) > 0
    drew_registered = by_source.get('registered', 0.0) > 0

    # ── No income shock -> runway is n/a, not a number ──────────────────
    # Runway measures time FROM a shock. A scenario with no dated income
    # shock (the auto-discovered baseline / "stay at current job") has
    # nothing to measure -- reporting a number would be a false precision.
    # Distinct from "engaged=False" (no living-cost budget) and from
    # "survives" (a shock the household weathered): here there is no shock.
    if shock_date is None:
        return RunwayResult(
            engaged=True,
            runway_months=None,
            runway_months_bracket=(None, None),
            stress_begins_months=None,
            survives_horizon_months=None,
            interpolated=False,
            method="n/a — no income shock declared (runway measures time from "
                   "a shock; this scenario has none)",
            first_ruin_year=ruin_year_idx,
            first_shortfall_year=shortfall_year_idx,
            relies_on_credit_facility=relies_on_credit,
            drew_registered=drew_registered,
        )

    # ── Stress-begins: months to first WORKING-LIFE shortfall ───────────
    stress_begins: Optional[float] = None
    if shortfall_year_idx is not None:
        stress_calendar = start_year + shortfall_year_idx - 1
        # Stress begins AT THE START of the shortfall year (the household was
        # solvent through the end of the prior year). No interpolation: this
        # is "when the cushion is first touched", a bracket lower bound.
        stress_begins = max(
            0.0, _months_between(shock_anchor, _start_of_year(stress_calendar)))

    # ── Survives-floor: months from shock to end of the last WORKING year ─
    # The household made it to retirement (or the horizon end) without a
    # working-life ruin. The floor is the working-life span, not the whole
    # horizon -- retirement is #707's domain, not runway.
    last_working = working[-1] if working else results[-1]
    last_year_idx = getattr(last_working, 'year', len(results))
    last_calendar = start_year + last_year_idx - 1
    survives_floor = max(
        0.0, _months_between(shock_anchor, _end_of_year(last_calendar)))

    # ── Headline: months to first WORKING-LIFE ruin (labelled interp) ───
    if ruin_year_idx is None:
        # No working-life ruin: the household survived the shock through
        # working life (reached retirement, or the horizon end). Runway is
        # "at least survives_floor" — NOT a finding of safety in retirement
        # (#707 owns that), and NOT infinity.
        return RunwayResult(
            engaged=True,
            runway_months=None,
            runway_months_bracket=(None, None),
            stress_begins_months=stress_begins,
            survives_horizon_months=survives_floor,
            interpolated=False,
            method="survives working life (no working-life ruin; retirement "
                   "is #707's domain)",
            first_ruin_year=None,
            first_shortfall_year=shortfall_year_idx,
            relies_on_credit_facility=relies_on_credit,
            drew_registered=drew_registered,
        )

    ruin_calendar = start_year + ruin_year_idx - 1
    lower = max(0.0, _months_between(shock_anchor, _start_of_year(ruin_calendar)))
    upper = max(lower, _months_between(shock_anchor, _end_of_year(ruin_calendar)))

    # The interpolation fraction: how far the liquid stock stretched inside
    # the ruin year, per the waterfall's own covered-vs-shortfall bookkeeping.
    # ruin_year_idx is 1-based -> trajectory row is results[ruin_year_idx - 1].
    ruin_row = (results[ruin_year_idx - 1]
                if 0 < ruin_year_idx <= len(results) else None)
    shortfall_r = float(getattr(ruin_row, 'solvency_shortfall', 0.0)
                        if ruin_row is not None else 0.0)
    covered_r = float(getattr(ruin_row, 'solvency_covered', 0.0)
                      if ruin_row is not None else 0.0)
    if shortfall_r > 0:
        frac = max(0.0, min(1.0, covered_r / shortfall_r))
    else:
        # ruined with a zero shortfall should not happen (ruined <=> the
        # waterfall exhausted against a positive shortfall), but if it
        # does, the only honest position is the lower bound: ruin at the
        # start of the ruin year.
        frac = 0.0

    point = lower + frac * (upper - lower)

    return RunwayResult(
        engaged=True,
        runway_months=point,
        runway_months_bracket=(lower, upper),
        stress_begins_months=stress_begins,
        survives_horizon_months=survives_floor,
        interpolated=True,
        method=("linear interpolation within the ruin year (uniform monthly "
                "burn); bracket is structural"),
        first_ruin_year=ruin_year_idx,
        first_shortfall_year=shortfall_year_idx,
        relies_on_credit_facility=relies_on_credit,
        drew_registered=drew_registered,
    )


# ── Reading the shock date off a config (issue #758) ────────────────────

def shock_date_from_members(members: Sequence[Dict]) -> Optional[date]:
    """The earliest ``from`` date among every member's dated income segments
    (#674) -- the calendar date the income shock begins.

    Duck-typed over the config's ``family.members`` list (DP#25: this module
    does not import ``SimulationConfig``). Returns ``None`` when no member
    carries any ``income_segments`` -- the baseline / no-shock case, for which
    runway is measured from the projection start (the day-one-shock
    convention the caller falls back to when ``shock_date`` is None).

    A member with an empty ``income_segments`` list contributes nothing; a
    member with no ``income_segments`` key at all contributes nothing. Both are
    real (the baseline scenario emits ``[]`` per #674's total-shape contract),
    so neither is an error here -- absence of EVERY segment is the no-shock
    signal, not a missing input.
    """
    earliest: Optional[date] = None
    for m in members or []:
        # `income_segments` is a list when present (#674's total-shape contract);
        # explicit default arg, never an `or []` coercion (DP#32 -- an empty
        # list is a real value: this member is on baseline income this run).
        for seg in m.get('income_segments', []):
            seg_from = seg.get('from')
            if seg_from is None:
                continue
            d = date.fromisoformat(seg_from)
            if earliest is None or d < earliest:
                earliest = d
    return earliest


# ── Shock-date sweep (issue #758: "now, or in a year") ─────────────────────

@dataclass(frozen=True)
class RunwayCurvePoint:
    """One point on the runway-vs-shock-date curve: the household's runway
    if the shock lands on ``shock_date``."""
    shock_date: date
    runway_months: Optional[float]
    runway_months_bracket: Tuple[Optional[float], Optional[float]]
    stress_begins_months: Optional[float]
    survives_horizon_months: Optional[float]
    engaged: bool
    unengaged_reason: Optional[str] = None

    def to_dict(self) -> Dict:
        lower, upper = self.runway_months_bracket
        return {
            'shock_date': self.shock_date.isoformat(),
            'runway_months': self.runway_months,
            'runway_months_bracket': [lower, upper] if self.engaged else None,
            'stress_begins_months': self.stress_begins_months,
            'survives_horizon_months': self.survives_horizon_months,
            'engaged': self.engaged,
            'unengaged_reason': self.unengaged_reason,
        }


def shift_income_scenario_dates(segments: Sequence[Dict],
                                 original_shock_date: date,
                                 new_shock_date: date) -> List[Dict]:
    """Translate a dated income-segment schedule (#674) so the shock that
    began on ``original_shock_date`` instead begins on ``new_shock_date`,
    preserving every segment's relative length and the gaps between them.

    The shock date is the swept dimension (issue #758): the same household
    has different runway for a shock today vs in 12 months, because assets
    have grown, debts amortized, and an obligation may have ended. Rather
    than ask the household to hand-author one ``decisions.income[]`` entry
    per shifted date, the sweep re-uses the household's ONE authored shock
    scenario and shifts its ``from``/``to`` dates by the same delta — the
    engine then re-simulates each variant and ``compute_runway`` reads the
    months off each trajectory (DP#9: one shock-shape, swept by data).

    Pure (DP#3): returns a new list, never mutates the input. ``to=None``
    (the explicit "permanent" spelling, #674) is preserved as None.
    """
    delta = (new_shock_date - original_shock_date).days
    shifted: List[Dict] = []
    for seg in segments:
        seg_from = date.fromisoformat(seg['from'])
        new_from = seg_from + _days(delta)
        seg_to = seg.get('to')
        new_to = None if seg_to is None else date.fromisoformat(seg_to) + _days(delta)
        shifted.append({
            'kind': seg['kind'],
            'amount': seg['amount'],
            'from': new_from.isoformat(),
            'to': new_to.isoformat() if new_to is not None else None,
        })
    return shifted


# ── Worst-case bridge for the model_fidelity caveats (issue #758) ───────────
#
# Mirrors decumulation.worst_drawdown_shortfall / assumptions.decumulation_shortfall:
# the registry is built at import time and an Approximation is frozen, so a
# caveat that names a RUN-SPECIFIC runway fact (which scenario drew the credit
# line) can only read what the run wrote onto the cfg. optimize.main records
# the worst-case (shortest-runway) scenario's verdict onto assumptions.runway
# after the run and before any surface renders, so the same finding reaches
# the console header, TXT, JSON, and HTML -- one spelling (DP#9), every
# surface (DP#32).

def worst_runway_summary(results: Sequence[Dict]) -> Dict:
    """The shortest-runway scenario's verdict, for the model_fidelity caveats.

    "Shortest" = the smallest finite ``runway_months`` (the household runs
    out soonest). A scenario that SURVIVES the horizon (runway_months=None)
    is longer than any finite one, so it never wins the worst case. When no
    scenario is engaged, an all-False summary is returned -- the caveats stay
    silent, and the caller's NOT-CHECKED notice is the only honest output.
    """
    worst: Optional[Dict] = None
    worst_months: Optional[float] = None
    worst_label: Optional[str] = None
    for r in results:
        rw = r.get('runway')
        if rw is None or not rw.get('engaged'):
            continue
        months = rw.get('runway_months')
        # None (survives) is "longer" than any finite runway -- skip it for
        # the worst case (a surviving scenario is not the household's danger).
        if months is None:
            continue
        if worst is None or months < worst_months:
            worst = rw
            worst_months = months
            worst_label = r.get('income_scenario_label')
    if worst is None:
        return {
            'engaged': False, 'runway_months': None,
            'relies_on_credit_facility': False, 'drew_registered': False,
            'scenario_label': None,
        }
    return {
        'engaged': True,
        'runway_months': worst.get('runway_months'),
        'relies_on_credit_facility': worst.get('relies_on_credit_facility', False),
        'drew_registered': worst.get('drew_registered', False),
        'scenario_label': worst_label,
    }


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def format_runway(rw: Dict) -> str:
    """One-line rendering of a runway verdict for console/TXT/HTML (issue #758).

    THE single spelling of this rendering (DP#9): both ``optimize.py`` (the
    console report) and ``output_plugins.py`` (TXT/HTML) import it rather
    than each inventing its own. Mirrors the solvency verdict's inline
    markers (UNCHECKED / RUIN): an un-engaged row prints ``UNCHECKED`` (DP#32
    -- never "0 mo"); a row whose household survives the horizon prints the
    floor as ``>=N mo (survives)``; a ruined row prints its interpolated
    months with the bracket, labelled as an interpolation so a reader never
    mistakes a year-granular point estimate for an exact month.

    Takes a serialized runway dict (``RunwayResult.to_dict()``) so it works
    on ranking-row data that has already crossed the data/serialization
    boundary, not on the frozen dataclass.
    """
    if not rw.get('engaged'):
        return "UNCHECKED"
    runway = rw.get('runway_months')
    method = rw.get('method')
    if method is None:
        method = ''
    if 'no income shock' in method:
        return "n/a (no shock)"
    if runway is None:
        floor = rw.get('survives_horizon_months')
        if floor is not None:
            return f">={floor:.0f} mo (survives working life)"
        return "never (survives)"
    bracket = rw.get('runway_months_bracket')
    if bracket is None:
        bracket = (None, None)
    lower, upper = bracket
    bracket_txt = ""
    if lower is not None and upper is not None and (upper - lower) > 1e-6:
        bracket_txt = f" [{lower:.0f}-{upper:.0f}]"
    return f"~{runway:.0f} mo{bracket_txt} (interp)"


def format_curve_point(p: 'RunwayCurvePoint') -> Tuple[str, str]:
    """Render one sweep-curve point's (runway, bracket) cell text for the
    console sweep table (issue #758). Pure (DP#3) and the single spelling of
    the per-point rendering -- extracted so every branch (unengaged /
    survives / ruined) is unit-testable without driving a full optimizer
    sweep per branch. Returns ``(runway_txt, bracket_txt)``."""
    if not p.engaged:
        return "UNCHECKED", "-"
    if p.runway_months is None:
        floor = p.survives_horizon_months
        if floor is not None:
            return f">={floor:.0f} mo (survives)", "-"
        return "never (survives)", "-"
    lo, hi = p.runway_months_bracket
    bracket_txt = (f"[{lo:.0f}-{hi:.0f}]"
                   if lo is not None and hi is not None else "-")
    return f"~{p.runway_months:.0f} mo (interp)", bracket_txt


def runway_curve(results_by_shock_date: Sequence[Tuple[date, Sequence]],
                  start_year: Optional[int] = None) -> List[RunwayCurvePoint]:
    """Build the runway-vs-shock-date curve from a sequence of
    ``(shock_date, trajectory)`` pairs — one per swept shock date.

    This is a THIN fold over ``compute_runway``: the caller (the optimizer /
    exploration layer) is responsible for producing one simulated trajectory
    per shock date (each a full ``run`` under a date-shifted income
    scenario). This function only converts each trajectory into its
    :class:`RunwayCurvePoint`, so the curve itself is directly testable from
    data rather than only observable by parsing a printed chart (mirrors
    ``summarize_solvency`` / ``summarize_drawdown_shortfall``'s split of pure
    fold from printing).
    """
    points: List[RunwayCurvePoint] = []
    for shock_date, trajectory in results_by_shock_date:
        r = compute_runway(trajectory, shock_date=shock_date, start_year=start_year)
        points.append(RunwayCurvePoint(
            shock_date=shock_date,
            runway_months=r.runway_months,
            runway_months_bracket=r.runway_months_bracket,
            stress_begins_months=r.stress_begins_months,
            survives_horizon_months=r.survives_horizon_months,
            engaged=r.engaged,
            unengaged_reason=r.unengaged_reason,
        ))
    return points