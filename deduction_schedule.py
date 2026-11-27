"""The best RRSP deduction schedule for declared contributions (issue #488).

An RRSP contribution's deduction need not be claimed in the year it is made:
the unclaimed amount carries forward indefinitely (ITA s.146(5), Schedule 7)
and can be claimed in any later year. The question a household with a lump-sum
contribution actually asks is therefore not "deduct now or later?" but "in
which year(s), and how much in each, so that the tax saved is greatest?".

:func:`best_deduction_schedule` answers it as a pure function (DP#3) over the
contributor's projected income path -- the per-year taxable income the engine
values that contributor's RRSP deduction against, and that year's brackets --
recorded by the run itself on ``YearResult.rrsp_deduction_base``. Nothing here
recomputes income or tax rules; it only allocates the pool across the path.

The allocation
--------------
Deducting ``d`` from income ``I`` saves ``tax(I) - tax(I - d)``, a concave
piecewise-linear function of ``d`` (the first dollar comes off the top
bracket). Each year therefore offers a stack of bands, one per bracket between
the floor and ``I``, each worth its bracket's rate per dollar. A contribution
can be claimed only from its contribution year on, which bounds the claims
made up to any year ``t`` by what was contributed up to ``t``. Taking bands in
descending rate order (earliest year first on a tie), each as large as that
bound still allows, is optimal for this structure (a separable concave
objective over release-date constraints).

The floor is ``SimulationConfig.deduct_later_bracket_target`` when the
household declared one (it does not want income deducted below it), and
otherwise the lowest taxed income, below which a deduction saves nothing.

What it does not do -- said on every surface that prints it
-----------------------------------------------------------
* It maximises the NOMINAL (undiscounted) tax saved. A dollar of tax saved in
  a later year is counted at par.
* The income path is the engine's projection for the run; it already includes
  retirement income once a member retires.
* It treats the declared contributions and the contributor's opening
  undeducted position as the only deductions against that path. RRSP
  contributions made by the modelled savings flow are deducted by the engine's
  own policy and also use the same income; their total is reported beside the
  schedule (``savings_flow_rrsp``) so the overlap is visible.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from rrsp_ledger import lowest_taxed_floor
from tax_calculator import deduction_value

#: Below this a residual is float noise, not money (matches rrsp_ledger).
_EPS = 1e-6

#: The ledger roles each contributor's deductions are claimed under.
_ROLES = {'primary': ('primary', 'spousal'), 'spouse': ('spouse',)}
_SLOTS = {'primary': ('primary_rrsp', 'spousal_rrsp'), 'spouse': ('spouse_rrsp',)}


def _bands(income: float, floor: float, brackets: List[Dict]) -> List[tuple]:
    """``(rate, width)`` for each positive-rate bracket slice of (floor, income]."""
    out = []
    for b in brackets:
        upper = b['max'] if b['max'] else float('inf')
        lo = max(floor, b['min'])
        hi = min(income, upper)
        if hi > lo and b['rate'] > 0:
            out.append((b['rate'], hi - lo))
    return out


def best_deduction_schedule(pool: List[Dict], path: List[Dict],
                            bracket_target: float) -> Dict:
    """The claim schedule that maximises the nominal tax saved.

    Args:
        pool: ``{'amount', 'available_from'}`` per contribution, the latter a
            0-based index into ``path`` (an opening position is 0).
        path: per projection year, ``{'income', 'brackets'}``.
        bracket_target: income floor below which nothing is deducted; ``<= 0``
            means the lowest taxed income of each year's brackets.

    Returns ``{'claims': [{'index', 'amount', 'tax_saved'}...], 'tax_saved',
    'claimed', 'unclaimed'}`` -- ``unclaimed`` is what no year of the path can
    absorb above the floor, still carried forward at the horizon.
    """
    n = len(path)
    if n == 0:
        raise ValueError("best_deduction_schedule: empty income path (DP#32).")
    released = [0.0] * n
    for p in pool:
        if not 0 <= p['available_from'] < n:
            raise ValueError(
                f"best_deduction_schedule: a contribution is available from "
                f"index {p['available_from']}, outside the {n}-year path (DP#32).")
        released[p['available_from']] += p['amount']
    cum_released = []
    acc = 0.0
    for r in released:
        acc += r
        cum_released.append(acc)

    floors = [bracket_target if bracket_target > 0
              else lowest_taxed_floor(y['brackets']) for y in path]
    bands = []
    for i, y in enumerate(path):
        for rate, width in _bands(y['income'], floors[i], y['brackets']):
            bands.append((-rate, i, width))
    bands.sort()

    claimed = [0.0] * n
    cum_claimed = [0.0] * n
    for _neg_rate, i, width in bands:
        headroom = min(cum_released[t] - cum_claimed[t] for t in range(i, n))
        x = min(width, headroom)
        if x <= _EPS:
            continue
        claimed[i] += x
        for t in range(i, n):
            cum_claimed[t] += x

    return _priced(claimed, path, acc)


def claim_as_available(pool: List[Dict], path: List[Dict],
                       bracket_target: float) -> Dict:
    """The reference schedule: claim everything as soon as it is available,
    each year down to the same floor -- what a contributor who never defers
    would get. Same return shape as :func:`best_deduction_schedule`."""
    n = len(path)
    released = [0.0] * n
    for p in pool:
        released[p['available_from']] += p['amount']
    claimed = [0.0] * n
    pending = 0.0
    for i, y in enumerate(path):
        pending += released[i]
        floor = (bracket_target if bracket_target > 0
                 else lowest_taxed_floor(y['brackets']))
        x = min(pending, max(0.0, y['income'] - floor))
        claimed[i] = x
        pending -= x
    return _priced(claimed, path, sum(released))


def _priced(claimed: List[float], path: List[Dict], total: float) -> Dict:
    claims = []
    saved = 0.0
    for i, amount in enumerate(claimed):
        if amount <= _EPS:
            continue
        value = deduction_value(path[i]['income'], amount, path[i]['brackets'])
        saved += value
        claims.append({'index': i, 'amount': amount, 'tax_saved': value})
    got = sum(c['amount'] for c in claims)
    return {'claims': claims, 'tax_saved': saved, 'claimed': got,
            'unclaimed': max(0.0, total - got)}


def summarize_deduction_schedule(config, results) -> Optional[Dict]:
    """Per contributor, the best schedule for the run's declared RRSP
    contributions plus that contributor's opening undeducted position.

    ``None`` when the household declared no RRSP one-off contribution --
    absence is absence (DP#32). Raises ``KeyError`` when contributions are
    declared but the run did not record its income path (a caller that did not
    fold the engine's annual step), never schedules against a zero income.
    """
    declared = [c for c in config.registered_contributions if c['plan'] == 'rrsp']
    if not declared:
        return None
    start = config.start_year
    path_by_role = {
        role: [{'income': r.rrsp_deduction_base[role],
                'brackets': r.rrsp_deduction_base['brackets']} for r in results]
        for role in _ROLES}
    target = config.deduct_later_bracket_target
    out: Dict[str, Dict] = {}
    for role, ledger_roles in _ROLES.items():
        mine = [c for c in declared if c['role'] in ledger_roles]
        if not mine:
            continue
        member = config.member_by_role(role, {})
        opening = member.get('rrsp_undeducted_contributions')
        pool = [{'amount': c['amount'], 'available_from': c['year'] - start}
                for c in mine]
        declared_pool = [{'amount': c['amount'],
                          'available_from': c['deduct_from_year'] - start}
                         for c in mine]
        if opening:
            pool.append({'amount': opening, 'available_from': 0})
            declared_pool.append({'amount': opening, 'available_from': 0})
        # A deduct_from beyond the projection is carried past the horizon by
        # the household's own election: clip it out of the as-declared pool
        # and report it as unclaimed there.
        n = len(results)
        beyond = sum(p['amount'] for p in declared_pool if p['available_from'] >= n)
        declared_pool = [p for p in declared_pool if p['available_from'] < n]
        path = path_by_role[role]
        best = best_deduction_schedule(pool, path, target)
        asap = claim_as_available(pool, path, target)
        as_declared = claim_as_available(declared_pool, path, target)
        as_declared['unclaimed'] += beyond
        declared_total = sum(c['amount'] for c in mine)
        savings_flow = sum(r.contributions[slot]
                           for r in results for slot in _SLOTS[role]) - declared_total
        out[role] = {
            'contributions': [{'id': c['id'], 'year': c['year'],
                               'amount': c['amount'],
                               'deduct_from_year': c['deduct_from_year']}
                              for c in mine],
            'opening_undeducted': opening if opening else 0.0,
            'floor': ('deduct_later_bracket_target' if target > 0
                      else 'lowest_taxed_income'),
            'bracket_target': target,
            'best': _with_years(best, start),
            'claim_as_available': _with_years(asap, start),
            'as_declared': _with_years(as_declared, start),
            'gain_vs_claim_as_available': best['tax_saved'] - asap['tax_saved'],
            'gain_vs_as_declared': best['tax_saved'] - as_declared['tax_saved'],
            'savings_flow_rrsp': max(0.0, savings_flow),
        }
    return out


def _with_years(schedule: Dict, start: int) -> Dict:
    schedule = dict(schedule)
    schedule['claims'] = [{'year': start + c['index'], 'amount': c['amount'],
                           'tax_saved': c['tax_saved']}
                          for c in schedule['claims']]
    return schedule


def render_deduction_schedule(summary: Optional[Dict]) -> List[str]:
    """Plain-text lines for the CLI and the Markdown report; ``[]`` when there
    is nothing to schedule."""
    if not summary:
        return []
    lines = ["BEST RRSP DEDUCTION SCHEDULE (declared contributions, issue #488)"]
    for role, s in summary.items():
        lines.append(f"  {role}:")
        for c in s['contributions']:
            when = ('claimed from the contribution year'
                    if c['deduct_from_year'] == c['year']
                    else f"deduction deferred to {c['deduct_from_year']}")
            lines.append(f"    contribution {c['id']}: ${c['amount']:,.0f} in "
                         f"{c['year']} ({when})")
        if s['opening_undeducted']:
            lines.append(f"    opening undeducted position: "
                         f"${s['opening_undeducted']:,.0f}")
        floor = (f"${s['bracket_target']:,.0f} (deduct_later_bracket_target)"
                 if s['floor'] == 'deduct_later_bracket_target'
                 else "the lowest taxed income")
        lines.append(f"    floor: never deduct below {floor}")
        best = s['best']
        lines.append("    best schedule:")
        for c in best['claims']:
            lines.append(f"      {c['year']}: deduct ${c['amount']:,.0f} "
                         f"-> saves ${c['tax_saved']:,.0f}")
        if best['unclaimed'] > _EPS:
            lines.append(f"      still carried at the horizon: "
                         f"${best['unclaimed']:,.0f}")
        lines.append(f"    tax saved: best ${best['tax_saved']:,.0f}; "
                     f"as declared ${s['as_declared']['tax_saved']:,.0f}; "
                     f"claiming as soon as available "
                     f"${s['claim_as_available']['tax_saved']:,.0f}")
        if s['savings_flow_rrsp'] > _EPS:
            lines.append(f"    note: the modelled savings flow also contributes "
                         f"${s['savings_flow_rrsp']:,.0f} to this contributor's "
                         f"RRSP over the run; its deductions use the same "
                         f"income and are not part of this schedule")
    lines.append("  Nominal tax saved, undiscounted, against the run's projected "
                 "income; the refund itself reaches wealth only through a HELOC "
                 "paydown today (#306).")
    return lines
