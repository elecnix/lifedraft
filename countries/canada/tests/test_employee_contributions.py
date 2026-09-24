"""Issue #289: unit tests for ``countries/canada/employee_contributions.py``,
the employee CPP/QPP, EI and QPIP premiums and their s.60(e) / s.118.7
relief.

Every expected figure is a PUBLISHED maximum, cited beside it -- never a
premium formula re-typed in the test (DP#11). The 2025 figures are chosen
because an earner at the 2025 YMPE (71,300) sits at every employee maximum
the agencies publish:

* CPP 2025, CRA "CPP contribution rates, maximums and exemptions": YMPE
  $71,300, basic exemption $3,500, rate 5.95%, maximum employee contribution
  $4,034.10.
  https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/canada-pension-plan-cpp/cpp-contribution-rates-maximums-exemptions.html
  The first additional (enhanced) part is 1% of the 5.95%, so its maximum is
  $678.00 (CRA line 22215) and the base part is $3,356.10 (line 30800).
* CPP2 2025, CRA "CPP2 contribution rates and maximums": YAMPE $81,200, 4%,
  maximum employee contribution $396.
  https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
* EI 2025, CRA "EI premium rates and maximums": MIE $65,700; federal rate
  1.64%, maximum employee premium $1,077.48; Quebec rate 1.31%, maximum
  $860.67.
  https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/employment-insurance-ei/ei-premium-rates-maximums.html
* QPP 2025, Retraite Quebec: rate 6.40%, maximum employee contribution
  $4,339.20 (of which the first additional plan is 1%, $678.00).
* QPIP 2025, Revenu Quebec: employee rate 0.494%, MIE $98,000 -- so an
  earner at $71,300 pays 0.494% of it, $352.22.

DP#4/DP#15: fabricated round incomes, role-free unit calls.
"""

import ast
import dataclasses
import json
import os

import pytest

from countries.canada.employee_contributions import (
    NON_PAYROLL_KINDS,
    PAYROLL_INSURABLE_KINDS,
    EmployeeContributions,
    employee_contribution_breakdown,
    is_payroll_insurable,
)
from tax_data import TaxDataProvider

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

YMPE_2025 = 71_300


@pytest.fixture(scope='module')
def provider():
    return TaxDataProvider()


class _Patched(TaxDataProvider):
    """A real provider whose records for ``provinces`` carry ``overrides`` --
    the smallest honest way to model a record that lacks a fact."""

    def __init__(self, provinces, **overrides):
        super().__init__()
        self._provinces = set(provinces)
        self._overrides = overrides

    def get_year_data(self, year, country='canada', province='quebec'):
        data = super().get_year_data(year, country, province)
        if province in self._provinces:
            return dataclasses.replace(data, **self._overrides)
        return data


# ── Published oracles ────────────────────────────────────────────────────

def test_ontario_2025_at_ympe_matches_cra_maxima(provider):
    b = employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, provider)
    assert b.pension_base + b.pension_first_additional == pytest.approx(4034.10, abs=0.01)
    assert b.pension_base == pytest.approx(3356.10, abs=0.01)
    assert b.pension_first_additional == pytest.approx(678.00, abs=0.01)
    assert b.pension_second_additional == 0.0
    assert b.ei_premium == pytest.approx(1077.48, abs=0.01)
    assert b.qpip_premium == 0.0
    assert b.s60e_deduction == pytest.approx(678.00, abs=0.01)
    assert b.s118_7_credit_base == pytest.approx(4433.58, abs=0.01)
    assert b.total_premiums == pytest.approx(4034.10 + 1077.48, abs=0.01)


def test_ontario_postal_code_resolves_to_the_same_breakdown(provider):
    assert (employee_contribution_breakdown(YMPE_2025, 'on', 2025, provider)
            == employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, provider))


@pytest.mark.parametrize('province', ['quebec', 'qc', 'Quebec'])
def test_quebec_2025_at_ympe_charges_qpp_reduced_ei_and_qpip(provider, province):
    b = employee_contribution_breakdown(YMPE_2025, province, 2025, provider)
    assert b.pension_base + b.pension_first_additional == pytest.approx(4339.20, abs=0.01)
    assert b.pension_first_additional == pytest.approx(678.00, abs=0.01)
    assert b.pension_base == pytest.approx(3661.20, abs=0.01)
    assert b.ei_premium == pytest.approx(860.67, abs=0.01)   # NOT 1,077.48
    assert b.qpip_premium == pytest.approx(352.22, abs=0.01)
    assert b.s60e_deduction == pytest.approx(678.00, abs=0.01)
    assert b.s118_7_credit_base == pytest.approx(3661.20 + 860.67 + 352.22, abs=0.01)


def test_cpp2_above_yampe_2025(provider):
    """At or above the 2025 YAMPE the CPP2 maximum ($396) is charged, all of
    it inside the s.60(e) deduction; between the YMPE and the YAMPE it is 4%
    of the excess over the YMPE ($5,000 x 4% = $200 at $76,300)."""
    above = employee_contribution_breakdown(90_000, 'ontario', 2025, provider)
    assert above.pension_second_additional == pytest.approx(396.00, abs=0.01)
    assert above.s60e_deduction == pytest.approx(678.00 + 396.00, abs=0.01)
    between = employee_contribution_breakdown(76_300, 'ontario', 2025, provider)
    assert between.pension_second_additional == pytest.approx(200.00, abs=0.01)


def test_2026_ceilings_and_quebec_rate_match_published_figures(provider):
    """2026 is the first year every real run prices, and every projected
    year copies it, so a wrong 2026 record mis-prices the whole horizon.

    * CRA 2026: YMPE $74,600, rate 5.95%, maximum employee CPP $4,230.45;
      AYMPE $85,000, CPP2 4%, maximum $416 (pages cited in the module
      docstring). The repo carried $81,900 as the 2026 YAMPE until #289.
    * Retraite Quebec 2026: basic plan 5.3% + first additional plan 1%
      (6.3%, down from 6.4%), second ceiling $85,000 at 4%; its worked
      example charges an employee $2,930 on $50,000 of earnings.
      https://www.retraitequebec.gouv.qc.ca/en/professionals-employers/employer/your-role-quebec-pension-plan/contributions-quebec-pension-plan-qpp
    """
    on = employee_contribution_breakdown(74_600, 'ontario', 2026, provider)
    assert on.pension_base + on.pension_first_additional == pytest.approx(4230.45, abs=0.01)
    on_top = employee_contribution_breakdown(90_000, 'ontario', 2026, provider)
    assert on_top.pension_second_additional == pytest.approx(416.00, abs=0.01)

    qc_example = employee_contribution_breakdown(50_000, 'quebec', 2026, provider)
    assert (qc_example.pension_base + qc_example.pension_first_additional
            == pytest.approx(2930, abs=0.5))
    qc_top = employee_contribution_breakdown(90_000, 'qc', 2026, provider)
    assert qc_top.pension_second_additional == pytest.approx(416.00, abs=0.01)


def test_credit_value_is_lowest_rates_with_quebec_abatement(provider):
    """The s.118.7 credit is worth the lowest federal rate (plus Ontario's
    lowest rate, ON428), and for a Quebec resident only (1 - abatement) of
    the federal rate with no Quebec credit. Read the rates off the provider
    (not re-typed) and check the relation the module must satisfy."""
    fed = provider.get_year_data(2025, 'canada', 'federal')
    on = provider.get_year_data(2025, 'canada', 'ontario')
    qc = provider.get_year_data(2025, 'canada', 'quebec')
    b_on = employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, provider)
    b_qc = employee_contribution_breakdown(YMPE_2025, 'quebec', 2025, provider)
    assert b_on.s118_7_credit_value == pytest.approx(
        b_on.s118_7_credit_base
        * (fed.federal_brackets[0].rate + on.provincial_brackets[0].rate))
    assert b_qc.s118_7_credit_value == pytest.approx(
        b_qc.s118_7_credit_base
        * fed.federal_brackets[0].rate * (1 - qc.provincial_abatement))


@pytest.mark.parametrize('province', ['ontario', 'quebec'])
@pytest.mark.parametrize('income', [0, 0.0, -5, -50_000])
def test_zero_and_negative_employment_income_is_all_zero(provider, province, income):
    b = employee_contribution_breakdown(income, province, 2025, provider)
    assert b == EmployeeContributions.zero()
    assert all(v == 0.0 for v in dataclasses.asdict(b).values())


# ── Loud failure on missing data (DP#32) ─────────────────────────────────

@pytest.mark.parametrize('value', [None, 0, 0.0])
@pytest.mark.parametrize('province', ['ontario', 'quebec'])
def test_missing_ei_mie_raises(province, value):
    p = _Patched({'federal'}, ei_max_insurable_earnings=value)
    with pytest.raises(ValueError, match='ei_max_insurable_earnings'):
        employee_contribution_breakdown(YMPE_2025, province, 2025, p)


@pytest.mark.parametrize('value', [None, 0, 0.0])
def test_missing_ei_rate_raises(value):
    p = _Patched({'federal'}, ei_employee_rate=value)
    with pytest.raises(ValueError, match='ei_employee_rate'):
        employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, p)


@pytest.mark.parametrize('value', [None, 0.0])
@pytest.mark.parametrize('province', ['quebec', 'qc'])
def test_missing_quebec_reduced_ei_rate_raises(province, value):
    """No fall back to the general rate: a Quebec employee priced at 1.64%
    would pay the wrong premium silently."""
    p = _Patched({'quebec', 'qc'}, ei_employee_rate_provincial_plan=value)
    with pytest.raises(ValueError, match='ei_employee_rate_provincial_plan'):
        employee_contribution_breakdown(YMPE_2025, province, 2025, p)


@pytest.mark.parametrize('field', ['qpip_employee_rate', 'qpip_max_insurable_earnings'])
def test_missing_qpip_data_raises(field):
    p = _Patched({'quebec', 'qc'}, **{field: 0.0})
    with pytest.raises(ValueError, match=field):
        employee_contribution_breakdown(YMPE_2025, 'quebec', 2025, p)


def test_missing_first_additional_rate_raises():
    with pytest.raises(ValueError, match='cpp_first_additional_rate'):
        employee_contribution_breakdown(
            YMPE_2025, 'ontario', 2025,
            _Patched({'federal'}, cpp_first_additional_rate=None))
    with pytest.raises(ValueError, match='qpp_first_additional_rate'):
        employee_contribution_breakdown(
            YMPE_2025, 'quebec', 2025,
            _Patched({'quebec'}, qpp_first_additional_rate=None))


@pytest.mark.parametrize('province', ['ontario', 'quebec'])
def test_provincial_credit_flag_none_raises(province):
    p = _Patched({province}, provincial_payroll_credit=None)
    with pytest.raises(ValueError, match='provincial_payroll_credit'):
        employee_contribution_breakdown(YMPE_2025, province, 2025, p)


def test_province_granting_the_credit_without_brackets_raises():
    p = _Patched({'ontario'}, provincial_brackets=[])
    with pytest.raises(ValueError, match='grants a payroll credit'):
        employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, p)


def test_empty_federal_brackets_raises():
    """No 15%/14.5% fallback rate for the credit (tax_calc's _load_fed_data
    has one; this module must not)."""
    p = _Patched({'federal'}, federal_brackets=[])
    with pytest.raises(ValueError, match='no brackets'):
        employee_contribution_breakdown(YMPE_2025, 'ontario', 2025, p)


def test_quebec_zero_qpp_rate_raises():
    p = _Patched({'quebec'}, qpp_rate=0.0)
    with pytest.raises(ValueError, match='QPP rate'):
        employee_contribution_breakdown(YMPE_2025, 'quebec', 2025, p)


def test_unsupported_province_raises(provider):
    with pytest.raises(ValueError):
        employee_contribution_breakdown(YMPE_2025, 'bc', 2025, provider)


# ── Income-kind classification is total over the schema enum ─────────────

def test_is_payroll_insurable_is_total():
    with open(os.path.join(_REPO, 'schema', 'defs', 'people.json')) as f:
        enum = set(json.load(f)['$defs']['income_kind']['enum'])
    assert PAYROLL_INSURABLE_KINDS | NON_PAYROLL_KINDS == enum
    assert not (PAYROLL_INSURABLE_KINDS & NON_PAYROLL_KINDS)
    for kind in enum:
        assert is_payroll_insurable(kind) == (kind == 'employment'), kind
    with pytest.raises(ValueError, match='not classified'):
        is_payroll_insurable('pension')


# ── Year-versioned data reaches projected years ──────────────────────────

@pytest.mark.parametrize('year', [2030, 2045, 2071])
def test_projected_year_carries_ei_fields(year):
    p = TaxDataProvider()
    fed, fed26 = (p.get_year_data(y, 'canada', 'federal') for y in (year, 2026))
    qc, qc26 = (p.get_year_data(y, 'canada', 'quebec') for y in (year, 2026))
    on = p.get_year_data(year, 'canada', 'ontario')
    assert fed.ei_employee_rate == fed26.ei_employee_rate is not None
    assert fed.cpp_first_additional_rate == fed26.cpp_first_additional_rate is not None
    assert qc.ei_employee_rate_provincial_plan == qc26.ei_employee_rate_provincial_plan is not None
    assert qc.qpp_first_additional_rate == qc26.qpp_first_additional_rate is not None
    assert qc.provincial_payroll_credit is False
    assert on.provincial_payroll_credit is True
    # The ceiling is indexed by the same factor as the YMPE.
    assert fed.ei_max_insurable_earnings > fed26.ei_max_insurable_earnings
    assert (fed.ei_max_insurable_earnings / fed26.ei_max_insurable_earnings
            == pytest.approx(fed.cpp_max_pensionable / fed26.cpp_max_pensionable,
                             rel=1e-6))
    # The CPP/QPP basic exemption is fixed at $3,500 by statute, never indexed.
    assert fed.cpp_exemption == fed26.cpp_exemption == 3500
    # And a projected-year breakdown prices every premium (no silent $0).
    for prov in ('ontario', 'quebec'):
        b = employee_contribution_breakdown(80_000, prov, year, p)
        assert b.pension_base > 0 and b.ei_premium > 0


def test_cached_record_without_payroll_keys_parses_to_none():
    """A cache file that predates the payroll fields must parse to None (the
    module then raises), never to a silent 0."""
    parsed = TaxDataProvider()._parse_cached(
        {'year': 2030, 'country': 'canada', 'province': 'federal',
         'federal_brackets': [], 'provincial_brackets': []})
    for name in ('ei_employee_rate', 'ei_max_insurable_earnings',
                 'ei_employee_rate_provincial_plan', 'cpp_first_additional_rate',
                 'qpp_first_additional_rate', 'provincial_payroll_credit'):
        assert getattr(parsed, name) is None, name


# ── No rate literal in the rule (DP#2/#12/#20) ───────────────────────────

def test_no_rate_literals_in_the_rule():
    path = os.path.join(_REPO, 'countries', 'canada', 'employee_contributions.py')
    with open(path) as f:
        tree = ast.parse(f.read())
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)}
    bad = [(n.lineno, n.value) for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
           and not isinstance(n.value, bool) and n.value not in (0, 1)
           and id(n) not in docstrings]
    assert bad == []
