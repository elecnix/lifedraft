"""Decumulation shortfall — the retirement drawdown runs out of money before
the projection horizon ends (issue #707).

This is the founding defect class this repo exists to kill: the engine
silently substitutes zero. A missing input becomes ``0``; an unimplemented
rule becomes a no-op; and here, a bankrupt year becomes just another number.
A plan that exhausts its assets in year 19 of a 45-year horizon completed
without complaint and printed a confident terminal estate (``retirement_income
= 44,216`` against a ``205,784`` net target, ``drawdown_net_delivered = 0``,
every account at ``0``), because nothing on ``YearResult`` signalled the gap.

``YearResult.drawdown_shortfall`` (set in ``simulation_rules.apply_retirement_drawdown``)
is the per-year fact: the NET spending gap left unfunded because every
drawable account was exhausted. This module folds a whole trajectory's
``YearResult`` list into the few facts a household actually needs to hear --
*did the money run out, when, and by how much* -- so the reporting layer and
the optimizer can surface them as DATA, never by raising (issue #657's
``except Exception: score = -inf`` would swallow an exception and rank a
bankrupt plan as merely a bad one).

Pure fold (DP#3), duck-typed over ``YearResult`` attributes rather than
importing the dataclass, so this module stays free of any dependency on
``simulation_config`` (DP#25: dependencies point inward -- this is the inner
layer and must not reach back out). Mirrors ``liquidation_waterfall
.summarize_solvency`` (#679): the two shortfalls are distinct -- solvency is
the cash-flow identity against declared ``household_budget.annual_living_costs``,
this is the retirement drawdown against ``retirement.spending_target`` -- and
a trajectory may hit either, both, or neither.
"""

from typing import Dict, List, Sequence


def summarize_drawdown_shortfall(results: Sequence) -> Dict:
    """Fold a trajectory's ``YearResult`` list into the decumulation-shortfall
    facts a household needs (issue #707).

    Returns::

        {
          'engaged':            bool,   # any year asked for a drawdown (target > 0)
          'exhausted':          bool,   # any year recorded a drawdown_shortfall
          'first_shortfall_year': int | None,   # YearResult.year of the first shortfall
          'first_shortfall_gap':  float,        # the gap that year (target - delivered)
          'shortfall_years':    int,            # count of years with a shortfall
          'total_unmet':        float,          # sum of drawdown_shortfall across the trajectory
        }

    ``engaged`` is False when no year ever requested a drawdown (the household
    never retired within the horizon, or supplied no ``spending_target``): the
    module never ran, and a caller that prints "0 shortfall years" for an
    un-engaged run would be reporting the most dangerous falsehood this
    codebase exists to prevent (DP#32), so the flag is returned first and the
    caller must branch on it -- exactly as ``summarize_solvency`` does for the
    cash-flow identity.
    """
    if not results:
        return {
            'engaged': False, 'exhausted': False,
            'first_shortfall_year': None, 'first_shortfall_gap': 0.0,
            'shortfall_years': 0, 'total_unmet': 0.0,
        }

    engaged = any(getattr(r, 'drawdown_net_target', 0.0) > 0 for r in results)
    shortfall_rows = [r for r in results if getattr(r, 'drawdown_shortfall', 0.0) > 0]
    first = shortfall_rows[0] if shortfall_rows else None

    return {
        'engaged': engaged,
        'exhausted': bool(shortfall_rows),
        # `year` is the 1-indexed relative offset the engine stamps on each
        # YearResult (see simulation_state), not a calendar year -- the caller
        # labels it, mirroring summarize_solvency's first_shortfall_year.
        'first_shortfall_year': getattr(first, 'year', None) if first else None,
        'first_shortfall_gap': getattr(first, 'drawdown_shortfall', 0.0) if first else 0.0,
        'shortfall_years': len(shortfall_rows),
        'total_unmet': sum(getattr(r, 'drawdown_shortfall', 0.0) for r in results),
    }


def worst_drawdown_shortfall(scenario_results: Sequence) -> Dict:
    """Across a ranked list of scenario result dicts (each carrying a
    ``'drawdown_shortfall'`` summary from ``summarize_drawdown_shortfall``),
    the one that exhausts earliest -- the fact a run-wide caveat must name.

    Returns the summary dict of the earliest-exhausting scenario that is
    actually exhausted, or an all-False summary when none are. "Earliest"
    beats "largest gap": a plan that runs out in year 19 is a worse answer
    to "can I retire?" than one that runs out in year 40 with a bigger final
    gap, regardless of the gap's size.

    DP#32: a scenario whose drawdown was never engaged is NOT collapsed into
    "not exhausted" -- it is absent from this comparison, because a run that
    never retired has not been found SAFE, it has not been CHECKED.
    """
    exhausted = [
        r for r in scenario_results
        if shortfall_of(r) is not None
        and shortfall_of(r).get('exhausted')
    ]
    if not exhausted:
        return {
            'engaged': False, 'exhausted': False,
            'first_shortfall_year': None, 'first_shortfall_gap': 0.0,
            'shortfall_years': 0, 'total_unmet': 0.0,
        }
    earliest = min(
        exhausted,
        key=lambda r: (r['drawdown_shortfall'].get('first_shortfall_year')
                       if r['drawdown_shortfall'].get('first_shortfall_year') is not None
                       else float('inf')))
    return earliest['drawdown_shortfall']


def shortfall_of(r):
    """The ``drawdown_shortfall`` summary on a scenario result dict, or None
    when the row carries none (an older/synthetic row). Explicit absence test,
    not ``.get(k) or {}`` (DP#32: a present-but-empty dict is a value, not an
    absence to overwrite)."""
    if not isinstance(r, dict):
        return None
    ds = r.get('drawdown_shortfall')
    return ds if ds is not None else None

def ranking_key(r: Dict, score: float):
    """Sort key for ranking scenario result dicts (issue #707): a trajectory
    that exhausted its assets before the horizon always sorts BELOW one that
    did not, regardless of headline score -- because a bankrupt plan and a
    solvent plan are not comparable on terminal net worth, and ranking them
    side by side on a single score is exactly how a bankrupt plan wins.

    Use with ``reverse=True``: the key is ``(not_exhausted, score)`` so a
    non-exhausted trajectory (1) outranks an exhausted one (0), and within
    each group the higher score ranks first. The numeric score itself is
    NOT mutated -- it remains the ledger fact the household is owed (DP#32:
    the figure is reported honestly, just no longer crowned the winner when
    the trajectory that produced it went bankrupt).

    A result dict that carries no ``'exhausted'`` flag (a synthetic/older
    row) is treated as not-exhausted, so this key is a safe drop-in for the
    bare ``key=lambda r: r.get('net_benefit', 0)`` it replaces.
    """
    exhausted = bool(r is not None and isinstance(r, dict)
                     and r.get('exhausted', False))
    return (0 if exhausted else 1, score)
