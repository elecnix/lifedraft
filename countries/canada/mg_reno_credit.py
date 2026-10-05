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

from dataclasses import dataclass, field, replace
from datetime import date
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
    birth_year: Optional[int] = None

    def qualifies(self, year: int) -> bool:
        """s.122.92(3), evaluated against a DATE (DP#1).

        The statute gives two routes, and they are not symmetric:

        (a) the person has **reached 65** by the end of the renovation-period
            taxation year; or
        (b) the person is **18 or older** at the end of that year **and** is
            eligible for the disability tax credit under s.118.3.

        The 18+ floor applies to route (b) ONLY. Without it a DTC-eligible
        minor made a renovation qualify, which the statute does not allow --
        worth up to the full $7,500.
        """
        if year < MHRTC_FIRST_YEAR:
            return False
        if self.birth_year is not None:
            age = year - self.birth_year
            if age >= 65:
                return True          # route (a)
            if age < 18:
                return False         # s.122.92(3)(b): the 18+ floor
            return self.dtc_eligible  # route (b)
        # No birth year recorded: fall back to the caller's resolved flags.
        return self.reached_65_by_year_end or self.dtc_eligible


@dataclass
class MultigenerationalRenovation:
    """A dated qualifying expenditure on a secondary unit."""

    year: Optional[int]
    qualifying_expenditures: float
    has_secondary_unit: bool = False
    qualifying_person_ids: Tuple[str, ...] = field(default_factory=tuple)
    prior_qualifying_renovations: int = 0


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

def mg_reno_from_property(
    facts: Optional[dict],
    qualifiers: Sequence[QualifyingIndividual],
) -> Optional[MultigenerationalRenovation]:
    """Build the renovation from a property's ``mg_reno`` block.

    DP#16: returns None when the block is ABSENT, so the module does not
    participate and the household is byte-identical to one that never did this
    work. A block that is PRESENT but malformed refuses (DP#32) rather than
    returning None, because "did not renovate" and "documented a renovation we
    cannot price" must not look the same.

    The year comes from ``renovation_date`` -- a DATE, never a bare year or an
    assumed 2026 (DP#1/DP#2).
    """
    if facts is None:
        return None
    if not facts:
        raise ValueError(
            "a property's mg_reno block is present but empty. That is neither "
            "'no qualifying renovation' nor a document we can price -- refuse "
            "rather than credit $0 (DP#32)."
        )

    raw_date = facts.get("renovation_date")
    if not raw_date:
        raise ValueError(
            "mg_reno.renovation_date is required and absent. The credit "
            "belongs to the taxation year of completion, so there is no year to "
            "price it without (DP#32)."
        )
    try:
        renovation_date = date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise ValueError(
            f"mg_reno.renovation_date {raw_date!r} is not an ISO date "
            f"(YYYY-MM-DD): {exc} (DP#32)."
        ) from exc

    expenditures = facts.get("qualifying_expenditures")
    if expenditures is None:
        raise ValueError(
            "mg_reno.qualifying_expenditures is required and absent. Without "
            "it the credit has no B term, and B=0 would be indistinguishable "
            "from an unknown outlay (DP#32)."
        )
    if isinstance(expenditures, bool) or not isinstance(expenditures, (int, float)):
        raise ValueError(
            f"mg_reno.qualifying_expenditures must be a number of dollars; "
            f"{expenditures!r} is {type(expenditures).__name__}. Booleans are "
            f"not money (DP#32)."
        )

    person_ids = facts.get("qualifying_person_ids")
    if not person_ids:
        raise ValueError(
            "mg_reno.qualifying_person_ids must name at least one qualifying "
            "individual. An empty list is a gap in the document, not a "
            "household that fails to qualify (DP#32)."
        )

    return MultigenerationalRenovation(
        year=renovation_date.year,
        qualifying_expenditures=float(expenditures),
        has_secondary_unit=bool(facts.get("has_secondary_unit", False)),
        qualifying_person_ids=tuple(person_ids),
        prior_qualifying_renovations=int(
            facts.get("prior_qualifying_renovations", 0)),
    )


def qualifiers_from_people(members: Sequence[dict]) -> list:
    """Build the qualifying individuals from the household's people block.

    The 65 test is DATE-COMPUTED against the renovation year (DP#1): a member's
    eligibility is a function of their birth date and the year, never a stored
    boolean that can go stale against a different as_of.

    A member is DTC-eligible when they are 18+ AND their own record says so.
    The engine has no independent disability assessment, so the declaration is
    a FACT about the household (DP#2), not an inference -- and it is never
    defaulted to False for an absent field on a person the credit names.
    """
    out: list = []
    for member in members:
        person_id = member.get("person_id")
        if not person_id:
            # This member cannot be named by any mg_reno block, because a
            # block names people by person_id. They are simply not about this
            # credit; refusing here would let an unrelated record stop every
            # run in the repo (DP#16). A NAMED person who resolves to nobody
            # IS refused, in mg_reno_credit_for_year.
            continue
        birth_year = member.get("birth_year")
        if birth_year is None:
            # Without a birth year the DATE-computed age test cannot be
            # evaluated. Skipping would read "unknown" as "not qualifying",
            # which is a plausible wrong zero (DP#32).
            raise ValueError(
                f"member {person_id!r} has no birth_year, so the s.122.92 age "
                f"test (reached 65 by the end of the renovation-period year) "
                f"cannot be evaluated. Refusing rather than assuming (DP#32)."
            )
        if isinstance(birth_year, bool) or not isinstance(birth_year, int):
            raise ValueError(
                f"member {person_id!r} has birth_year={birth_year!r} "
                f"({type(birth_year).__name__}); a year is required and a "
                f"boolean is not one (DP#32)."
            )
        out.append(QualifyingIndividual(
            person_id=person_id,
            birth_year=birth_year,
            reached_65_by_year_end=False,   # resolved per-year by qualifies()
            dtc_eligible=bool(member.get("dtc_eligible", False)),
        ))
    return out


def mg_reno_credit_for_year(
    renovation: Optional[MultigenerationalRenovation],
    qualifiers: Sequence[QualifyingIndividual],
) -> float:
    """The credit, resolving the age test against the renovation's own year."""
    if renovation is None:
        return 0.0
    # DP#32: a person the credit NAMES must resolve to a real household
    # member. If one does not, the document is inconsistent -- we cannot tell
    # whether they qualify, and crediting $0 would be indistinguishable from a
    # household that does not qualify.
    known = {q.person_id for q in qualifiers}
    for named in renovation.qualifying_person_ids:
        if named not in known:
            raise ValueError(
                f"the {renovation.year} renovation names qualifying person "
                f"{named!r}, who is not in the household's people block. Either "
                f"the person_id is wrong or the person is missing; both are "
                f"document gaps, and crediting $0 would look exactly like a "
                f"household that does not qualify (DP#32)."
            )
    resolved = [
        replace(q, reached_65_by_year_end=(
            q.birth_year is not None
            and renovation.year is not None
            and renovation.year - q.birth_year >= 65))
        for q in qualifiers
        if q.person_id in set(renovation.qualifying_person_ids)
    ]
    return multigenerational_reno_credit(
        renovation, resolved,
        prior_renovations=renovation.prior_qualifying_renovations)
