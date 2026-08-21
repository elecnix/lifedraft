#!/usr/bin/env python3
"""Pure config-reading member helpers (DP#25 data layer).

These three helpers read the raw config dict's ``family.members`` list and
derive structural facts (which member is the primary/spouse, how many adults
accumulate, how many years the horizon spans) WITHOUT touching any simulation
machinery -- no tax brackets, no return models, no strategy engine. They are
therefore data-layer utilities (DP#25 layer 1) that the scenario, simulation,
and reporting layers may all depend on inward.

They previously lived in ``simulation_config.py`` (simulation layer), which
forced the scenario layer (``scenario_discovery.py``) to import from the
simulation layer to reach them -- a DP#25 outward dependency (issue #998).
Relocating them here inverts that: ``simulation_config`` now imports them from
this lower layer (simulation -> data, inward), and ``scenario_discovery``
imports them from here directly (scenario -> data, inward).

Behaviour is byte-identical: the function bodies are moved verbatim, and every
existing call site is updated to the new single spelling (DP#9 -- no shim, no
re-export from ``simulation_config``).
"""

from typing import Dict, List, Optional


def find_member_by_role(members: List[Dict], role: str, default=None):
    """The single seam (#699) resolving a role label to its member dict.

    Every "which dict is the primary/spouse" lookup in the engine, optimizer,
    scenario and reporting layers goes through here (directly, or via the
    :meth:`SimulationConfig.member_by_role` method that delegates to it) instead
    of an inline ``next(m for m in members if m.get('role') == ...)``. That
    gives the multi-generation rewrite (#643) exactly one place to change when
    identity stops being a role string and becomes a relationship-graph
    traversal -- today it resolves by role and returns the very same object the
    inline idiom did (a representation change, not a behaviour change).

    ``default`` is returned when no member has that role. It is an explicit
    argument (not ``x or DEFAULT``, DP#32) so a caller that needs the historical
    ``{}`` sentinel passes its own fresh ``{}`` at the call site.
    """
    return next((m for m in members if m.get('role') == role), default)


def adult_members(members: List[Dict]) -> List[Dict]:
    """The adult members in canonical order: primary first, then spouse, then
    any additional accumulating adults in declared order (#699/#899).

    Excludes children (``role == 'child'``). Absent primary/spouse roles are
    simply omitted, so a lone primary yields ``[primary]``.

    Issue #899 (part a): the two-slot compute is uncapped to N ACCUMULATING
    adults, so this seam no longer caps at the primary couple -- an extra adult
    (any non-child member beyond primary/spouse, admitted by
    ``input_contract`` only when it is a pure accumulator across the horizon)
    follows the couple, preserving primary-then-spouse iteration order. For a
    two-adult household there are no such extras, so this returns exactly
    ``[primary, spouse]`` -- byte-identical to the pre-#899 seam.
    """
    primary = find_member_by_role(members, 'primary')
    spouse = find_member_by_role(members, 'spouse')
    couple = [m for m in (primary, spouse) if m is not None]
    couple_ids = {id(m) for m in couple}
    extras = [m for m in members
              if m.get('role') != 'child' and id(m) not in couple_ids]
    return couple + extras


def projection_span(*, horizon_age: Optional[int], start_year: int,
                    members: List[Dict], projection_years: int) -> int:
    """Years to simulate, derived from the horizon (DP#1, DP#3).

    ``assumptions.horizon_age`` is the source of truth for "plan until the
    primary turns N" — a *rule*, not a count. The span is computed against the
    primary's ``birth_year`` and ``start_year`` so it stays correct when either
    moves; a hand-counted ``projection_years`` would go stale (DP#1).

    The last simulated year is the one in which the primary reaches
    ``horizon_age``, so the span is inclusive of both endpoints::

        span = horizon_age - (start_year - birth_year) + 1

    Falls back to the explicit ``projection_years`` when no ``horizon_age`` is
    given, or when the primary has no ``birth_year`` and the horizon therefore
    cannot be dated (DP#13: defaults are fallbacks, not opinions).

    Args:
        horizon_age: age the primary reaches in the final simulated year.
        start_year: first simulated calendar year.
        members: family members (the one with role 'primary' dates the horizon).
        projection_years: explicit span, used as the fallback.

    Returns:
        Number of years to simulate (always >= 1).
    """
    if not horizon_age:
        return projection_years
    primary = find_member_by_role(members, 'primary')
    birth_year = (primary or {}).get('birth_year')
    if not birth_year:
        return projection_years
    age_at_start = start_year - birth_year
    return max(1, horizon_age - age_at_start + 1)