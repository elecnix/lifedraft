#!/usr/bin/env python3
"""Epic #795 bite 2 characterization test (DP#9 -- behavior-preserving refactor).

The per-member tuition tax credit wiring was spelled TWICE in the fold's
prologue (``simulate_year`` and ``_run_monthly``); bite 2 collapses it into a
single shared helper (``simulation._tuition_credits_for``) both time-steps
call. This test guards the NON-ZERO path the golden fixture cannot: the
golden household declares NO tuition, so its credit is 0 everywhere and it
exercises only the zero path. A fabricated household that DOES declare
tuition (both taxed members, overlapping years, Quebec so the provincial
credit fires too) must reproduce its year-by-year trajectory byte-for-byte
against the origin/main baseline captured BEFORE the refactor -- for BOTH
the yearly and monthly time-step folds (the monthly path is the second
spelling the bite unifies).

The tuition credit's observable effect is on ``after_tax_income`` (the credit
is subtracted from each member's ``tax_on_income``, floored at 0): the
baseline's after_tax_income varies by year as the declared tuition_by_year
changes, then stabilizes once no more tuition is declared. ``total_assets`` is
0 here (no investment return, no savings) -- the signal is the tax/after-tax
figures, not the balance sheet.

The baseline below was captured on origin/main (d9a6283, which includes bite 1)
by running this exact fabricated household. Round numbers, role-based names
(DP#4/DP#15) -- no real data.

Run: uv run pytest tests/test_epic795_bite2_tuition_credit_characterization.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter


def _tuition_household_config(time_step='yearly'):
    """A fabricated couple where BOTH taxed members declare tuition in
    overlapping years (primary 2026-2028, spouse 2027-2029) -- exercises the
    per-member credit wiring for both, with the non-refundable floor mattering
    once the credit exceeds a low bracket. Quebec (so the QC provincial
    tuition credit fires too). Round numbers, role-based names (DP#4/DP#15)."""
    members = [
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
         'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0,
         'tuition_by_year': {2026: 12_000, 2027: 8_000, 2028: 5_000}},
        {'role': 'spouse', 'birth_year': 1982, 'gross_income': 45_000,
         'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0,
         'tuition_by_year': {2027: 9_000, 2028: 11_000, 2029: 6_000}},
    ]
    return {
        'family': {'members': members, 'children': []},
        'accounts': {},
        'assumptions': {'start_year': 2026, 'projection_years': 8,
                        'investment_return': 0.0, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True,
                        'time_step': time_step},
        'portfolio': {'accounts': {}},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'amortization_years': 25,
                     'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'retirement': {'spending_target': 0, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
        # living_costs so the solvency identity + after_tax_income observable fire.
        'household_budget': {'living_costs': 50_000},
    }


def _run_tuition(time_step):
    cfg = SimulationConfig.from_dict(_tuition_household_config(time_step))
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()
TUITION_CHARACTERIZATION_BASELINE = {
    'yearly': [
        (1, 120000.0, 45000.0, 117928.91440000001, 165000.0, 0.0),
        (2, 120000.0, 45000.0, 119028.91440000001, 165000.0, 0.0),
        (3, 120000.0, 45000.0, 118808.91440000001, 165000.0, 0.0),
        (4, 120000.0, 45000.0, 116608.91440000001, 165000.0, 0.0),
        (5, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (6, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (7, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (8, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
    ],
    'monthly': [
        (1, 120000.0, 45000.0, 117928.91440000001, 165000.0, 0.0),
        (2, 120000.0, 45000.0, 119028.91440000001, 165000.0, 0.0),
        (3, 120000.0, 45000.0, 118808.91440000001, 165000.0, 0.0),
        (4, 120000.0, 45000.0, 116608.91440000001, 165000.0, 0.0),
        (5, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (6, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (7, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
        (8, 120000.0, 45000.0, 115288.91440000001, 165000.0, 0.0),
    ],
}


# Issue #289: this QC couple's net employee payroll cost, published here so
# the baseline add-back below is pinned to a constant rather than read back
# off the very YearResult being asserted (an add-back derived from the value
# under test cancels any premium change and stops constraining anything).
#
#   QPP   primary (74,600 - 3,500) x 6.30% = 4,479.30
#         + QPP2 (85,000 - 74,600) x 4%    =   416.00
#         + spouse (45,000 - 3,500) x 6.30% = 2,614.50   = 7,509.80
#   EI    primary min(120,000, 68,900) x 1.30% = 895.70
#         + spouse min(45,000, 68,900) x 1.30% = 585.00    = 1,480.70
#         (EI has no basic exemption: canada.ca, "Payroll Responsibilities
#          for Employers" -- "Unlike the CPP, there is no basic exemption and
#          no age limit for EI premiums")
#   QPIP  primary min(120,000, 103,000) x 0.430% = 442.90
#         + spouse min(45,000, 103,000) x 0.430% = 193.50  =   636.40
#   less ITA s.118.7 credit (base QPP + EI + QPIP, at the lowest federal
#   rate, de-aborated for Quebec)                                  = 1,566.89
#   ---------------------------------------------------------------------
#                                                             8,060.01
#
# Sources: Retraite Quebec QPP contribution rates and maximums (2026 MGA
# 74,600 / max. supplementaire 85,000; employee rate 5.3% + 1% = 6.30%);
# CRA "EI premium rates and maximums" (2026 MIE 68,900; Quebec employee rate
# 1.30%); Revenu Quebec "Employee Premium Under the QPIP" (2026 rate 0.430%,
# maximum insurable income 103,000).
NET_PAYROLL_2026 = 8060.01

_FIELDS = ('year', 'primary_income', 'spouse_income', 'after_tax_income',
           'total_family_income', 'total_assets')


@pytest.mark.parametrize('time_step', ['yearly', 'monthly'])
def test_tuition_household_trajectory_matches_origin_main(time_step):
    """Both time-step folds reproduce the origin/main trajectory for a
    household that declares tuition -- the per-member credit's effect on
    after_tax_income, every year."""
    results = _run_tuition(time_step)
    baseline = TUITION_CHARACTERIZATION_BASELINE[time_step]
    assert len(results) == len(baseline), (
        f"{time_step}: projected {len(results)} years, baseline has "
        f"{len(baseline)} -- the household's projection_years changed")
    for res, base in zip(results, baseline):
        # Issue #289: the baseline predates the employee payroll premiums, so
        # after_tax_income is now lower by this household's net payroll cost.
        # Two separate pins, because one combined derived add-back would
        # constrain nothing:
        #   1. the run's OWN payroll fields must net to the published constant
        #      -- this is what constrains the premiums and their relief;
        #   2. after_tax_income plus that SAME CONSTANT must reproduce the
        #      origin/main figure -- this is what constrains everything else.
        net_payroll = (res.payroll_pension_contributions + res.payroll_ei_premiums
                       + res.payroll_qpip_premiums - res.payroll_tax_relief)
        assert net_payroll == pytest.approx(NET_PAYROLL_2026, abs=0.01), (
            f"{time_step} year {res.year}: net payroll cost is {net_payroll!r}, "
            f"expected {NET_PAYROLL_2026!r} -- a premium rate, a ceiling or the "
            f"s.118.7 relief moved")
        actual = (res.year, res.primary_income, res.spouse_income,
                  res.after_tax_income + NET_PAYROLL_2026,
                  res.total_family_income, res.total_assets)
        for i, (got, exp) in enumerate(zip(actual, base)):
            if isinstance(exp, float):
                assert got == pytest.approx(exp), (
                    f"{time_step} year {res.year} {_FIELDS[i]}: got {got!r}, "
                    f"expected {exp!r} (origin/main baseline moved -- the "
                    f"tuition-credit extraction changed a number)")
            else:
                assert got == exp, (
                    f"{time_step} year {res.year} {_FIELDS[i]}: got {got!r}, "
                    f"expected {exp!r}")


def test_characterization_household_actually_claims_tuition():
    """Guard on the test's own premise: the household's after_tax_income must
    DIFFER from a no-tuition baseline (else the trajectory pin is vacuous --
    the credit never fired). The credit reduces tax, so after_tax_income with
    tuition declared must be HIGHER than without it in the years tuition is
    declared."""
    with_tuition = _run_tuition('yearly')
    # Build the no-tuition control by stripping tuition_by_year.
    cfg_dict = _tuition_household_config('yearly')
    for m in cfg_dict['family']['members']:
        m.pop('tuition_by_year', None)
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    without_tuition = sim.run()
    # Year 1 (2026): primary declares 12000 tuition -> credit reduces primary
    # tax -> after_tax_income rises vs the no-tuition control.
    assert with_tuition[0].after_tax_income > without_tuition[0].after_tax_income, (
        "declared tuition did not raise after_tax_income vs the no-tuition "
        "control -- the credit is not wired, so the characterization pin is "
        "vacuous")
    # Years with no tuition declared (year 5+, i.e. 2030+) must match the
    # control exactly (the credit is 0 there in both).
    assert with_tuition[4].after_tax_income == pytest.approx(without_tuition[4].after_tax_income)
