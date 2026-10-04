#!/usr/bin/env python3
"""CPP/QPP year-versioned parameters — the single ceiling lookup.

Every CPP/QPP ceiling in the engine (YMPE, YAMPE, the age-65 maximum, the
CPP2 age-65 maximum) is answered from here, for one year, for one plan.
Consumers call :func:`cpp_parameters`; they do not keep a ceiling table of
their own and they do not re-derive a fallback.

Design:

- DP#10: one module per government program. CPP and QPP share their
  contributory ceilings, so one module owns both plans' parameters and the
  plan only selects the age-65 maximum table.
- DP#12: the QPP figures are not restated here. Quebec owns them; the
  Quebec package registers them through :func:`register_qpp_max_benefit_65`
  at import time, the same shape ``tax_data.register_oas_fallback`` uses for
  the federal OAS fallback rows. This module owns the *interface*, the
  Quebec package owns the *data*.
- DP#20: every parameter is keyed by calendar year.
- DP#3: pure — the same (year, plan) always returns the same parameters.

The one rule for a year with no row
-----------------------------------

``cpp_parameters(y, p)`` resolves each ceiling against the rows that exist:

* a row for that exact year wins;
* otherwise the **nearest year that has a row** is used (ties resolve to the
  later year), so a year before the first row answers with the first row and
  a year after the last row answers with the last row;
* if the table holds no row at all for that quantity, the call **raises**
  ``ValueError`` — it never answers zero (DP#32).

Nearest-row carry-forward is the convention already used for year-versioned
data elsewhere in this repo (``tax_data.TaxDataProvider.get_year_data``
picks the nearest available year, and ``resp_rules`` warns loudly when it
does). It is deliberately *not* a growth projection: inventing "YMPE grows
2%/yr past the last published year" is an opinion about absent data (DP#13),
and an invented projection is free to be wrong in either direction. The
first such rule in this codebase projected YMPE for 2027 off the 2022 row
and produced a maximum *below* the published 2026 one.

References:
    https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit/amount.html
    CPP2 (AYMPE): https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
    QPP: https://www.retr.quebec.ca/en (see ``provinces/quebec/tax_data.py``)
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

from countries.canada.retirement import CPP_OAS_BY_YEAR

# The plans this module prices. QPP shares the contributory ceilings with
# CPP and differs only in the age-65 maximum (DP#10, DP#7: the mechanism,
# not the branded product).
CPP_PLANS = ("cpp", "qpp")

# The second additional plan (CPP2 / "enhanced" band) has no row before the
# year it began. Before it, YAMPE == YMPE: there is no band to contribute in.
CPP2_START_YEAR = 2024

# ── Historical YMPE (1966–2022) ─────────────────────────────────────────────
# Service Canada / CRA historical maxima. The engine models contributory
# periods that begin before 2023, so these rows are load-bearing, not
# archival: without them a 1975 earnings year has no YMPE at all.
# Source: Service Canada / CRA historical YMPE tables.
_HISTORICAL_YMPE: Dict[int, float] = {
    1966: 5000, 1967: 5000, 1968: 5100, 1969: 5200, 1970: 5300,
    1971: 5400, 1972: 5500, 1973: 5600, 1974: 6600, 1975: 7400,
    1976: 8300, 1977: 9300, 1978: 10400, 1979: 11700, 1980: 13100,
    1981: 14700, 1982: 16500, 1983: 18500, 1984: 20800, 1985: 23400,
    1986: 25800, 1987: 25900, 1988: 26500, 1989: 27700, 1990: 28900,
    1991: 30500, 1992: 32200, 1993: 33400, 1994: 34400, 1995: 34900,
    1996: 35400, 1997: 35800, 1998: 36900, 1999: 37400, 2000: 37600,
    2001: 38300, 2002: 39100, 2003: 39900, 2004: 40500, 2005: 41100,
    2006: 42100, 2007: 43700, 2008: 44900, 2009: 46300, 2010: 47200,
    2011: 48300, 2012: 50100, 2013: 51100, 2014: 52500, 2015: 53600,
    2016: 54900, 2017: 55300, 2018: 55900, 2019: 57400, 2020: 58700,
    2021: 61600, 2022: 64900,
}

# ── QPP age-65 maxima — registered by the Quebec package (DP#10, DP#12) ─────
#
# Empty until ``countries.canada`` imports the Quebec tax data and calls
# register_qpp_max_benefit_65(). Quebec owns these numbers; nothing here
# restates them.
_QPP_MAX_BENEFIT_65_BY_YEAR: Dict[int, float] = {}


def register_qpp_max_benefit_65(records: Iterable) -> None:
    """Register the QPP age-65 maxima from the Quebec package's own records.

    Args:
        records: Quebec ``TaxYearData`` records (e.g. ``QuebecTaxData.all_years()``).

    Raises:
        ValueError: a record carries no QPP maximum. Registering a zero would
            make a ``plan="qpp"`` estimate silently price on the CPP maximum
            (DP#32).
    """
    for record in records:
        value = record.qpp_max_benefit_65
        if not value > 0:
            raise ValueError(
                f"Quebec {record.year} record carries no QPP age-65 maximum "
                f"({value!r}). Refusing to register zero: a QPP estimate "
                f"priced on the CPP maximum is a silent swap of programs "
                f"(DP#32)."
            )
        _QPP_MAX_BENEFIT_65_BY_YEAR[int(record.year)] = float(value)


def qpp_max_benefit_65_years() -> Tuple[int, ...]:
    """Years for which a QPP age-65 maximum is registered."""
    return tuple(sorted(_QPP_MAX_BENEFIT_65_BY_YEAR))


@dataclass(frozen=True)
class CPPParameters:
    """One year of CPP (or QPP) parameters.

    Frozen data (DP#3): no behaviour, no hidden state.

    Attributes:
        year: The calendar year these parameters are for.
        plan: ``"cpp"`` or ``"qpp"``.
        ympe: Year's Maximum Pensionable Earnings.
        yampe: Second-ceiling maximum (equals ``ympe`` before 2024).
        max_benefit_65: Maximum annual retirement benefit at age 65.
        max_cpp2_benefit: Maximum annual CPP2 benefit at age 65 (0 before 2024).
    """

    year: int
    plan: str
    ympe: float
    yampe: float
    max_benefit_65: float
    max_cpp2_benefit: float


# ── Row lookup ──────────────────────────────────────────────────────────────

def _nearest(table: Dict[int, float], year: int, label: str) -> float:
    """The row for ``year``, else the row for the nearest year that has one.

    Ties resolve to the later year. Raises when the table is empty — a
    quantity with no row anywhere is missing data, not a zero (DP#32).
    """
    if not table:
        raise ValueError(
            f"No {label} row exists for any year, so none can be answered "
            f"for {year}. Load the year-versioned data rather than reading a "
            f"default (DP#32)."
        )
    if year in table:
        return float(table[year])
    nearest_year = min(table, key=lambda candidate: (abs(candidate - year), -candidate))
    return float(table[nearest_year])


def _federal_rows(key: str) -> Dict[int, float]:
    """The federal ``CPP_OAS_BY_YEAR`` rows projected onto one ceiling."""
    return {
        int(row_year): float(row[key])
        for row_year, row in CPP_OAS_BY_YEAR.items()
        if key in row
    }


def _historical_ympe_rows() -> Dict[int, float]:
    return {year: float(value) for year, value in _HISTORICAL_YMPE.items()}


def ympe_for_year(year: int) -> float:
    """YMPE for ``year`` — federal row, else the nearest row that exists.

    The 2023+ rows come from ``CPP_OAS_BY_YEAR`` (the federal fallback table
    in ``retirement.py``); 1966-2022 from the historical table above.
    """
    rows = _historical_ympe_rows()
    rows.update(_federal_rows("cpp_max_pensionable"))
    return _nearest(rows, year, "YMPE")


def yampe_for_year(year: int) -> float:
    """YAMPE (the second ceiling) for ``year``.

    A published ``cpp2_max_pensionable`` row always wins. Only a year with no
    such row *and* earlier than :data:`CPP2_START_YEAR` falls back to the
    YMPE — CPP2 did not exist then, so there was no band to contribute in and
    a wider ceiling would invent one. Reading the year before the table would
    discard a real row if one were ever published for a pre-2024 year.
    """
    rows = _federal_rows("cpp2_max_pensionable")
    if year in rows:
        return float(rows[year])
    if year < CPP2_START_YEAR:
        return ympe_for_year(year)
    return _nearest(rows, year, "YAMPE")


def max_benefit_65_for_year(year: int, plan: str = "cpp") -> float:
    """Maximum annual retirement benefit at 65 for ``year`` under ``plan``.

    ``plan="qpp"`` reads the Quebec-owned maxima from the earliest QPP year
    onwards. A year *before* the QPP table starts falls back to the federal
    maximum: inventing a QPP figure for 1990 would fabricate a program datum
    that did not exist (issue #390).
    """
    if plan == "qpp":
        if not _QPP_MAX_BENEFIT_65_BY_YEAR:
            raise ValueError(
                "No QPP age-65 maximum is registered. The Quebec package owns "
                "those numbers and must register them (register_qpp_max_benefit_65) "
                "before a QPP estimate can be priced; falling through to the CPP "
                "maximum here would be a silent swap of programs (DP#32)."
            )
        if year >= min(_QPP_MAX_BENEFIT_65_BY_YEAR):
            return _nearest(_QPP_MAX_BENEFIT_65_BY_YEAR, year,
                            "QPP max benefit at 65")
    return _nearest(_federal_rows("cpp_max_benefit_65"), year, "CPP max benefit at 65")


def max_cpp2_benefit_for_year(year: int) -> float:
    """Maximum annual CPP2 benefit at 65 for ``year``.

    A published ``cpp2_max_benefit`` row always wins. A year with no such row
    *and* earlier than :data:`CPP2_START_YEAR` is 0.0 — CPP2 did not exist,
    so no enhancement could have been earned. The year test is applied *after*
    the table so a published pre-2024 row is never discarded for zero.
    """
    rows = _federal_rows("cpp2_max_benefit")
    if year in rows:
        return float(rows[year])
    if year < CPP2_START_YEAR:
        return 0.0
    return _nearest(rows, year, "CPP2 max benefit")


def cpp_parameters(year: int, plan: str = "cpp") -> CPPParameters:
    """Every CPP/QPP ceiling for one year, under one plan.

    The one entry point for consumers. Pure (DP#3).

    Args:
        year: Calendar year (DP#1).
        plan: ``"cpp"`` or ``"qpp"``. The plan selects the age-65 maximum
            table only — the contributory ceilings are shared.

    Returns:
        A frozen :class:`CPPParameters`.

    Raises:
        ValueError: ``plan`` is not a known plan, or a ceiling has no row in
            any year. Neither answers a default (DP#32).
    """
    if plan not in CPP_PLANS:
        raise ValueError(
            f"Unknown pension plan {plan!r}. Known plans: {CPP_PLANS}. "
            f"Refusing to price a QPP resident on the CPP maximum, or the "
            f"reverse, by accident (DP#32)."
        )
    return CPPParameters(
        year=year,
        plan=plan,
        ympe=ympe_for_year(year),
        yampe=yampe_for_year(year),
        max_benefit_65=max_benefit_65_for_year(year, plan),
        max_cpp2_benefit=max_cpp2_benefit_for_year(year),
    )