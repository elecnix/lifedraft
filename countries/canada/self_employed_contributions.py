"""Issue #978: the mandatory Quebec self-employed contribution stack on net
business income, assembled from the existing calculators with one spelling
(DP#9 -- do NOT re-spell).

A self-employed Quebec earner owes -- on their net business income -- the
contributions an employer would have split with an employee, plus the
individual Health Services Fund. The fold's working-phase solvency identity
must charge this stack or it over-states the earner's disposable income (and
thus savings capacity) by the full stack vs an employee at the same gross
(#978). The calculators ALL EXIST and are correct; this module assembles them:

* QPP both halves (employee + employer): ``compute_cpp2_contribution`` returns
  ``total_self_employed`` (QPP1 = employee × 2, QPP2 = employee × 2). For a
  Quebec province it uses the QPP rate; for the rest of Canada it would use
  the CPP rate -- so passing the member's province through selects QPP vs CPP
  without a literal in core (DP#8).
* QPIP self-employed (both shares): ``quebec_qpip_premium(..., is_self_employed=True)``.
* The INDIVIDUAL Health Services Fund (line 446, max $1,000), payable by
  Quebec residents whose non-employment income -- which INCLUDES self-
  employment business income -- exceeds the exemption:
  ``quebec_health_services_fund_individual``. Per Revenu Québec / MSSS the
  individual HSF is capped at $1,000 and excludes employment income; this is
  the contribution issue #978 names ("individual Health Services Fund up to
  $1,000"), NOT the employer-equivalent self-employed FSS
  (``quebec_health_services_fund``, ~1.65% capped at YMPE) which is the
  employer-side FSS the CPA skill notes does not apply to self-employed
  earnings.

DP#32: a member with NO self-employment income
(``self_employment_income == 0``) owes the full stack on $0, which every
calculator returns as 0.0 (the calculators floor non-positive income at
zero), so the stack is byte-for-byte a no-op for an employee or a member with
no self-employment segment -- the golden household (employment income only) is
unchanged. Pure function (DP#3): same inputs → same output.

This module lives under ``countries.canada`` (DP#25 layer 1) so the
``'quebec'`` literal it gates on is jurisdiction code, NOT core logic -- the
core fold (``simulation.py``) calls it with the member's province and never
spells ``'quebec'`` itself (DP#8/DP#10: province is data, not a hardcoded
literal in jurisdiction-agnostic core).
"""

from __future__ import annotations

from countries.canada.cpp_sharing import compute_cpp2_contribution
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_qpip_premium,
    quebec_health_services_fund_individual,
)


def self_employed_contribution_stack(
        self_employment_income: float, province: str, year: int) -> float:
    """The mandatory self-employed contribution stack on net business income,
    as a single pre-savings cash outflow. See module docstring.

    Returns 0.0 for non-positive self-employment income (DP#32 absence-safe:
    an employee or a member with no self-employment segment owes nothing) and
    for a non-Quebec province (QPP-vs-CPP is the same calculator with the
    member's province passed through, but QPIP and the individual HSF are
    Quebec-only programs; outside Quebec only the QPP/CPP both-halves piece
    would apply, which is a separate, non-Quebec gap out of scope for #978).
    """
    if self_employment_income <= 0:
        return 0.0
    if province.lower() not in ('quebec', 'qc'):
        return 0.0

    qpp = compute_cpp2_contribution(
        self_employment_income, year=year, province='quebec',
    )['total_self_employed']
    qpip = quebec_qpip_premium(
        self_employment_income, is_self_employed=True, year=year,
    )
    hsf = quebec_health_services_fund_individual(
        self_employment_income, year=year,
    )
    return qpp + qpip + hsf