"""Multigenerational Home Renovation Tax Credit -- ITA s.122.92 (#369).

A qualifying renovation builds a self-contained secondary unit (its own
entrance, kitchen, bathroom and hydro meter) inside a principal residence, to
let an eligible relative live there. From 2023 the federal government credits a
share of the qualifying expenditure, REFUNDABLE.

    credit = A x B

**A** is the appropriate percentage for the year, defined by the statute as the
lowest federal bracket rate. That rate moves (0.15 for 2024, 0.145 for 2025,
0.14 for 2026), so it is READ from the year-versioned data and never hard-coded.

**B** is the least of $50,000 and the qualifying expenditures.

s.122.92(4) makes the credit a deemed payment ON ACCOUNT OF TAX, which is why
it is refundable and why the Quebec abatement does not reduce it. The CFFP
publication this scenario came from prints $3,131 for a $25,000 renovation in
2024 -- that is 15% net of the 16.5% Quebec abatement, which is the wrong
figure here for exactly that reason. The statutory 2024 value is $3,750.

Eligibility (s.122.92(3)) is DATE-COMPUTED (DP#1), not a stored flag: the
qualifying individual has reached 65 by the end of the renovation-period
taxation year, OR is 18 or older and eligible for the disability tax credit
under s.118.3.

Absence must fail loudly (DP#32). A silent zero here is indistinguishable from
"this household does not qualify", which is the exact confusion that lets a
wrong number survive.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

# The least of this and the qualifying expenditures.
RENOVATION_CREDIT_MAX = 50_000.0

# s.122.92(4): one qualifying renovation per qualifying individual, ever.
MHRTC_LIFETIME_CAP = 1

# The credit did not exist before 2023.
MHRTC_FIRST_YEAR = 2023


@dataclass
class QualifyingIndividual:
    """One person who might make a renovation qualifying."""

    person_id: str
    reached_65_by_year_end: bool = False
    dtc_eligible: bool = False

    def qualifies(self, year: int) -> bool:
        """s.122.92(3). Both routes, evaluated against a DATE (DP#1)."""
        if year < MHRTC_FIRST_YEAR:
            return False
        return self.reached_65_by_year_end or self.dtc_eligible


@dataclass
class MultigenerationalRenovation:
    """A dated qualifying expenditure on a secondary unit."""

    year: Optional[int]
    qualifying_expenditures: float
    has_secondary_unit: bool = False
    qualifying_person_ids: Tuple[str, ...] = field(default_factory=tuple)


def _lowest_federal_rate(year: int) -> float:
    """The statute's "appropriate percentage": the lowest federal bracket rate.

    Read from the year-versioned tax data (DP#20) rather than hard-coded, since
    it changes. A year with no brackets is a data gap, not a rate of zero.
    """
    from countries.canada.tax_calc import _load_fed_data

    try:
        _fed_data, lowest_rate, _bpa, _cea = _load_fed_data(year)
    except Exception as exc:  # noqa: BLE001 - re-raised with an accurate reason
        raise ValueError(
            f"the multigenerational renovation credit needs the federal "
            f"brackets for {year} to read the lowest bracket rate, and they "
            f"are unavailable ({exc}). Refusing rather than assuming a rate "
            f"(DP#32)."
        ) from exc
    return lowest_rate


def multigenerational_reno_credit(
    renovation: MultigenerationalRenovation,
    qualifiers: Sequence[QualifyingIndividual],
    prior_renovations: int = 0,
) -> float:
    """The s.122.92 credit for one renovation, in dollars.

    ``prior_renovations`` is how many qualifying renovations the qualifying
    individual has already claimed in their lifetime (DP#20: dated, not a
    boolean). At the lifetime cap the credit is zero, permanently.
    """
    if renovation.year is None:
        raise ValueError(
            "a multigenerational renovation must carry the year it was spent; "
            "the credit is a function of that year's lowest federal bracket "
            "rate (DP#32)."
        )
    if renovation.qualifying_expenditures < 0:
        raise ValueError(
            f"qualifying expenditures of {renovation.qualifying_expenditures!r} "
            f"are negative for {renovation.year}; a renovation cannot cost less "
            f"than nothing (DP#32)."
        )
    if prior_renovations < 0:
        raise ValueError(
            f"prior_renovations={prior_renovations!r} is negative; it counts "
            f"renovations already claimed and cannot be below zero (DP#32)."
        )
    if not renovation.qualifying_person_ids:
        raise ValueError(
            f"the {renovation.year} renovation names no qualifying individual. "
            f"An empty list is a gap in the document, not a household that "
            f"fails to qualify -- the two would be indistinguishable, so this "
            f"refuses (DP#32)."
        )

    # Not in force yet, or nothing to claim on.
    if renovation.year < MHRTC_FIRST_YEAR:
        return 0.0
    if not renovation.has_secondary_unit:
        return 0.0
    if renovation.qualifying_expenditures <= 0.0:
        return 0.0
    # s.122.92(4): one qualifying renovation per qualifying individual, ever.
    if prior_renovations >= MHRTC_LIFETIME_CAP:
        return 0.0

    named = set(renovation.qualifying_person_ids)
    if not any(q.person_id in named and q.qualifies(renovation.year)
               for q in qualifiers):
        return 0.0

    rate = _lowest_federal_rate(renovation.year)
    return rate * min(renovation.qualifying_expenditures, RENOVATION_CREDIT_MAX)