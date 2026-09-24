"""Issue #289: the mandatory EMPLOYEE payroll premiums on employment income,
and the income-tax relief the statute attaches to them -- one spelling beside
``self_employed_contributions.py`` (the #978 self-employed stack).

Every working year, an employee pays, out of gross pay:

* **CPP (or QPP in Quebec)** -- the base plan plus the first additional
  (enhanced) plan on earnings between the basic exemption and the YMPE, and
  the second additional contribution (CPP2 / QPP2) on earnings between the
  YMPE and the YAMPE. ``compute_cpp2_contribution`` already prices both tiers
  (QPP rate for a Quebec province); this module reuses it (DP#9) and splits
  its first tier into the base and first-additional parts with CRA's line
  22215 worksheet ratio (contribution x first-additional rate / total rate).
  Canada Pension Plan, R.S.C. 1985, c. C-8, Part I; Act respecting the
  Québec Pension Plan, CQLR c. R-9.
* **EI premiums** on insurable earnings up to the EI maximum insurable
  earnings -- at the general employee rate, or the reduced rate where a
  provincial plan replaces EI maternity/parental benefits (Employment
  Insurance Act, S.C. 1996, c. 23, ss. 67 and 69(2); Quebec/QPIP).
* **QPIP premiums** (Quebec only) -- ``quebec_qpip_premium(...,
  is_self_employed=False)`` (Act respecting parental insurance, CQLR c.
  A-29.011).

and the tax relief on them:

* **ITA s.60(e)** -- the enhanced part (first additional + CPP2/QPP2) is a
  DEDUCTION from income (line 22215; Quebec allows the same deduction, TP-1
  line 248).
* **ITA s.118.7** -- the base CPP/QPP part, EI premiums and QPIP (PPIP)
  premiums are a NON-REFUNDABLE CREDIT at the lowest federal rate (lines
  30800 / 31200 / 31205). For a Quebec resident the federal credit is worth
  (1 - Quebec abatement) of its face value, because the abatement is a
  percentage of basic federal tax. A province that grants its own credit for
  the same amounts (Ontario, ON428 lines 58240 / 58300) adds its lowest rate;
  Quebec does not (``provincial_payroll_credit`` is data, per record).

Every rate and ceiling comes from year-versioned ``TaxYearData`` (DP#2 /
DP#12 / DP#20); no rate literal lives here. A record that lacks a fact the
premium needs RAISES (DP#32) -- a missing rate must never price a premium at
$0, which is the silent flattering-the-household failure this issue fixes.
Zero or negative employment income owes nothing: every premium is a
percentage of positive earnings, so ``EmployeeContributions.zero()`` there is
the statutory value, not a fallback.

This module lives under ``countries.canada`` (DP#25 layer 1), so the
``'quebec'`` gate it applies is jurisdiction code; the core fold
(``simulation.py``) imports it lazily and never spells a province.
Pure functions (DP#3); the caller always passes its own provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from countries.canada.cpp_sharing import compute_cpp2_contribution
from countries.canada.provinces.quebec.quebec_credits import quebec_qpip_premium


# Which income-segment kinds are EMPLOYMENT earnings subject to employee
# payroll premiums (CPP/QPP pensionable + EI/QPIP insurable). Together the two
# sets must PARTITION $defs/income_kind's enum, exactly like
# earned_income.EARNED_INCOME_KINDS / NON_EARNED_INCOME_KINDS: an unclassified
# kind raises instead of silently owing no premium (the "unknown key defaulting
# to the favourable value" trap). Self-employment pays the #978 self-employed
# stack instead, never the employee premiums; EI benefits, investment, rental
# and other income are neither pensionable nor insurable earnings.
PAYROLL_INSURABLE_KINDS = frozenset({"employment"})
NON_PAYROLL_KINDS = frozenset(
    {"self_employment", "rental", "investment", "ei", "other"})


def is_payroll_insurable(kind: str) -> bool:
    """Is an income segment of this ``kind`` employment earnings that owe
    employee CPP/QPP, EI and QPIP premiums? Total by construction: an
    unclassified kind raises (DP#32)."""
    if kind in PAYROLL_INSURABLE_KINDS:
        return True
    if kind in NON_PAYROLL_KINDS:
        return False
    raise ValueError(
        f"income kind {kind!r} is not classified as payroll-insurable "
        f"employment earnings or not (issue #289). Known kinds: "
        f"{sorted(PAYROLL_INSURABLE_KINDS | NON_PAYROLL_KINDS)}. An "
        f"unclassified kind must not silently owe no CPP/QPP/EI premium -- "
        f"classify it in countries/canada/employee_contributions.py.")


@dataclass(frozen=True)
class EmployeeContributions:
    """One employee's payroll premiums and their tax relief for one year.

    ``pension_*`` are the CPP (or QPP) employee contributions by tier;
    ``s60e_deduction`` is the ITA s.60(e) deduction (first additional +
    second additional); ``s118_7_credit_base`` is the amount the s.118.7
    credit is computed on (base pension + EI + QPIP) and
    ``s118_7_credit_value`` its value against combined federal + provincial
    tax (``credit_base x (federal lowest rate x (1 - abatement) + provincial
    lowest rate where the province grants the credit)``). The credit is
    non-refundable: the caller floors tax at zero.
    """
    pension_base: float
    pension_first_additional: float
    pension_second_additional: float
    ei_premium: float
    qpip_premium: float
    s60e_deduction: float
    s118_7_credit_base: float
    s118_7_credit_value: float

    @property
    def pension_total(self) -> float:
        return (self.pension_base + self.pension_first_additional
                + self.pension_second_additional)

    @property
    def total_premiums(self) -> float:
        """Cash the employee pays out of gross pay this year."""
        return self.pension_total + self.ei_premium + self.qpip_premium

    @classmethod
    def zero(cls) -> 'EmployeeContributions':
        """No employment earnings -> no premiums and no relief."""
        return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _is_quebec(province: str) -> bool:
    return province.lower() in ("quebec", "qc")


def _require_positive(value, what: str, year: int, record: str) -> float:
    """A rate or ceiling the premium needs: None or <= 0 raises (DP#32)."""
    if value is None or value <= 0:
        raise ValueError(
            f"employee payroll premiums (issue #289): {what} is {value!r} in "
            f"the {record!r} tax record for {year}. Add it to the "
            f"year-versioned provider data with its source -- a missing "
            f"premium rate must not price the premium at $0.")
    return value


def employee_contribution_breakdown(
        employment_income: float, province: str, year: int,
        provider) -> EmployeeContributions:
    """Employee CPP/QPP, EI and QPIP premiums on ``employment_income`` for
    ``year`` in ``province``, with the s.60(e) deduction and the s.118.7
    credit. See the module docstring. ``provider`` is the caller's
    ``TaxDataProvider`` (its lookups are memoized; never built per call)."""
    if employment_income <= 0:
        return EmployeeContributions.zero()

    prov_key = province.lower()
    quebec = _is_quebec(prov_key)
    fed = provider.get_year_data(year, "canada", "federal")
    prov = provider.get_year_data(year, "canada", prov_key)

    # ── CPP/QPP: both tiers from the one existing calculator (DP#9). ──
    pension = compute_cpp2_contribution(
        employment_income, year=year, province=prov_key, provider=provider)
    tier1 = pension["cpp1_employee"]
    tier1_rate = _require_positive(
        pension["rate"], "the CPP/QPP contribution rate", year, prov.province)
    if quebec:
        first_rate = _require_positive(
            prov.qpp_first_additional_rate, "qpp_first_additional_rate",
            year, prov.province)
    else:
        first_rate = _require_positive(
            fed.cpp_first_additional_rate, "cpp_first_additional_rate",
            year, fed.province)
    # CRA line 22215 worksheet: the enhanced part of the first tier is
    # contribution x first-additional rate / total rate.
    first_additional = tier1 * first_rate / tier1_rate
    base = tier1 - first_additional
    second_additional = pension["cpp2_employee"]

    # ── EI: capped at the maximum insurable earnings. ──
    mie = _require_positive(
        fed.ei_max_insurable_earnings, "ei_max_insurable_earnings",
        year, fed.province)
    if quebec:
        ei_rate = _require_positive(
            prov.ei_employee_rate_provincial_plan,
            "ei_employee_rate_provincial_plan", year, prov.province)
    else:
        ei_rate = _require_positive(
            fed.ei_employee_rate, "ei_employee_rate", year, fed.province)
    ei = min(employment_income, mie) * ei_rate

    # ── QPIP (Quebec only). quebec_qpip_premium returns 0.0 when its data is
    # absent, so the rate and ceiling are validated here first (DP#32). ──
    if quebec:
        _require_positive(prov.qpip_employee_rate, "qpip_employee_rate",
                          year, prov.province)
        _require_positive(prov.qpip_max_insurable_earnings,
                          "qpip_max_insurable_earnings", year, prov.province)
        qpip = quebec_qpip_premium(
            employment_income, is_self_employed=False, year=year,
            provider=provider)
    else:
        qpip = 0.0

    # ── Tax relief. ──
    s60e = first_additional + second_additional
    credit_base = base + ei + qpip
    if not fed.federal_brackets:
        raise ValueError(
            f"employee payroll premiums (issue #289): the federal tax record "
            f"for {year} has no brackets, so the s.118.7 credit rate (the "
            f"lowest federal rate) is unknown.")
    federal_lowest = fed.federal_brackets[0].rate
    if prov.provincial_payroll_credit is None:
        raise ValueError(
            f"employee payroll premiums (issue #289): provincial_payroll_credit "
            f"is not set in the {prov.province!r} tax record for {year}. Record "
            f"whether the province grants its own non-refundable credit for "
            f"base CPP/QPP and EI premiums (with its source).")
    if prov.provincial_payroll_credit:
        if not prov.provincial_brackets:
            raise ValueError(
                f"employee payroll premiums (issue #289): the "
                f"{prov.province!r} record for {year} grants a payroll credit "
                f"but has no brackets to take its lowest rate from.")
        provincial_lowest = prov.provincial_brackets[0].rate
    else:
        provincial_lowest = 0.0
    credit_rate = (federal_lowest * (1 - prov.provincial_abatement)
                   + provincial_lowest)

    return EmployeeContributions(
        pension_base=base,
        pension_first_additional=first_additional,
        pension_second_additional=second_additional,
        ei_premium=ei,
        qpip_premium=qpip,
        s60e_deduction=s60e,
        s118_7_credit_base=credit_base,
        s118_7_credit_value=credit_base * credit_rate,
    )
