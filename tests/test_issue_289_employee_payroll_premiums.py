"""Issue #289: the fold charges every employee's CPP/QPP, EI and QPIP premiums
and applies their s.60(e) / s.118.7 relief.

Before #289 an employee kept gross pay minus bracket tax: the working-phase
solvency identity, savings capacity and runway all saw the premiums as cash
the household had. These tests drive ``FamilySimulation.run`` (never a
hand-built ``YearWorkingState``, DP#11) and assert against:

* PUBLISHED maxima (cited in countries/canada/tests/test_employee_contributions.py):
  CPP 2025 $4,034.10 (YMPE $71,300), EI 2025 $1,077.48 (federal) / $860.67
  (Quebec), QPP 2025 $4,339.20, QPIP 2025 0.494% of $71,300 = $352.22;
* engine-vs-engine counterfactuals: the same gross declared as an
  ``'other'``-kind segment is taxed identically (a precondition checked on
  origin/main: both kinds gave the same after_tax_income there) but owes no
  premium, so the difference between the two runs is the premium net of its
  relief -- nothing else.

DP#4/DP#15: fabricated round numbers and role-based names only.
"""

import logging
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig

YMPE_2025 = 71_300
LIVING_COSTS = 40_000   # declared so apply_solvency runs and after_tax_income is surfaced

PAYROLL_FIELDS = ('payroll_pension_contributions', 'payroll_ei_premiums',
                  'payroll_qpip_premiums', 'payroll_s60e_deduction',
                  'payroll_tax_relief')


def _premiums(r):
    return (r.payroll_pension_contributions + r.payroll_ei_premiums
            + r.payroll_qpip_premiums)


def _single_earner(province, segments, *, gross=YMPE_2025, start_year=2025,
                   years=1, time_step='yearly', living_costs=LIVING_COSTS,
                   savings_rate=0.0, tfsa_balance=0.0, extra=None):
    member = {"role": "primary", "birth_year": 1985, "gross_income": gross,
              "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
              "tfsa_balance": tfsa_balance, "income_segments": segments}
    member.update(extra or {})
    return SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        margin_available=0, start_year=start_year, province=province,
        savings_rate=savings_rate, living_costs=living_costs,
        time_step=time_step, family_members=[member])


def _full_year(kind, amount=YMPE_2025, start='2025-01-01'):
    return [{"kind": kind, "amount": amount, "from": start, "to": None}]


def _run(cfg):
    logging.disable(logging.WARNING)
    try:
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
    finally:
        logging.disable(logging.NOTSET)


# ── Fixtures shared with the origin/main baseline capture ───────────────

def retiree_household_config():
    """A retired couple (no employment income in any year): pensions and a
    drawdown, a declared budget so the solvency identity runs."""
    return SimulationConfig(
        projection_years=5, house_value=0, mortgage_balance=0,
        margin_available=0, start_year=2025, province='qc',
        savings_rate=0.0, living_costs=40_000,
        family_members=[
            {"role": "primary", "birth_year": 1955, "retirement_age": 65,
             "gross_income": 80_000, "rrsp_balance": 400_000,
             "tfsa_balance": 50_000, "rrsp_room_accumulated": 0,
             "tfsa_room_accumulated": 0},
            {"role": "spouse", "birth_year": 1957, "retirement_age": 65,
             "gross_income": 50_000, "rrsp_balance": 200_000,
             "tfsa_balance": 50_000, "rrsp_room_accumulated": 0,
             "tfsa_room_accumulated": 0},
        ])


def ei_only_household_config():
    """A working-age earner on EI benefits for the whole horizon."""
    return _single_earner('ontario', _full_year('ei', 28_000), years=3,
                          living_costs=30_000, tfsa_balance=100_000)


def investment_only_household_config():
    return _single_earner('quebec', _full_year('investment', 50_000), years=3,
                          living_costs=30_000, tfsa_balance=100_000)


def runway_household_config(kind):
    """Employed (or the ``'other'``-kind counterfactual) through 2025, then a
    job loss onto EI from 2026-01-01. Living costs just under the 2025
    after-tax pay before premiums, so the premiums are what tips 2025 into a
    shortfall."""
    segments = [
        {"kind": kind, "amount": YMPE_2025, "from": "2025-01-01", "to": "2026-01-01"},
        {"kind": "ei", "amount": 28_000, "from": "2026-01-01", "to": None},
    ]
    return _single_earner('ontario', segments, years=5, living_costs=54_000,
                          tfsa_balance=40_000)


# Captured on origin/main 594b6f8 (before #289) by running the fixtures above
# there: the retiree / zero-employment trajectories must not move by a bit
# (every other YearResult field was also compared, dataclasses.asdict per
# year, and was identical), and the runway fixture's employee reported the
# 'other'-kind counterfactual's figures because no premium existed.
RETIREE_BASELINE_TOTAL_ASSETS = [
    '704930.9456249999', '705086.4510737499', '704922.9793261025',
    '701082.1418689843', '692499.5409815202']
EI_ONLY_BASELINE_TOTAL_ASSETS = ['99526.0', '99158.82', '98765.93740000001']
RUNWAY_BASELINE = (17.897721964673124, 2)   # (runway_months, first_shortfall_year)


# ── Ontario employee at the YMPE ─────────────────────────────────────────

def test_ontario_employee_at_ympe_after_tax_drops_by_premiums_net_of_relief():
    """Ontario 2025, one earner at the YMPE ($71,300).

    Premiums (CRA published maxima): CPP $4,034.10 + EI $1,077.48 =
    $5,111.58.

    Relief, worked by hand from the published 2025 rates:
    * s.118.7 credit base = base CPP $3,356.10 + EI $1,077.48 = $4,433.58,
      credited at the lowest federal rate (14.5% for 2025) plus the lowest
      Ontario rate (5.05%, ON428) = 19.55% -> $866.77;
    * s.60(e) deduction = first additional CPP $678.00 at the combined
      marginal rate at $71,300 (federal 20.5% + Ontario 9.15% = 29.65%)
      -> $201.03;
    * total relief ~= $1,067.79, so the net cost is ~$4,043.79.
    """
    emp = _run(_single_earner('ontario', _full_year('employment')))[0]
    other = _run(_single_earner('ontario', _full_year('other')))[0]

    assert emp.payroll_pension_contributions == pytest.approx(4034.10, abs=0.01)
    assert emp.payroll_ei_premiums == pytest.approx(1077.48, abs=0.01)
    assert emp.payroll_qpip_premiums == 0.0
    assert emp.payroll_s60e_deduction == pytest.approx(678.00, abs=0.01)
    assert emp.payroll_tax_relief == pytest.approx(1067.79, abs=1.0)

    # The counterfactual owes nothing and is taxed on the same gross.
    assert all(getattr(other, f) == 0.0 for f in PAYROLL_FIELDS)
    assert emp.primary_marginal == other.primary_marginal
    # after_tax_income drops by exactly premiums - relief.
    assert other.after_tax_income - emp.after_tax_income == pytest.approx(
        _premiums(emp) - emp.payroll_tax_relief, abs=0.01)
    assert other.after_tax_income - emp.after_tax_income == pytest.approx(
        5111.58 - 1067.79, abs=1.0)


def test_payroll_relief_bounded_runs_on_every_run():
    """The per-year bound is a RUN-PATH invariant (#681), not a fixture."""
    import trajectory_invariants as t
    assert 'payroll_relief_bounded' in t.RUN_PATH_INVARIANTS
    assert 'payroll_relief_bounded' in t.all_invariant_names()
    for f in PAYROLL_FIELDS:
        assert f in t._ALL_NUMERIC_FIELDS


def test_payroll_relief_bounded_fires_on_a_corrupted_year():
    import trajectory_invariants as t
    from year_result import YearResult
    good = YearResult(year=1, employment_income=71_300.0,
                      payroll_pension_contributions=4034.10,
                      payroll_ei_premiums=1077.48,
                      payroll_s60e_deduction=678.0, payroll_tax_relief=1067.79)
    assert t.run_invariant('payroll_relief_bounded', [good]) == []
    relief_too_big = YearResult(year=1, employment_income=71_300.0,
                                payroll_pension_contributions=4034.10,
                                payroll_ei_premiums=1077.48,
                                payroll_s60e_deduction=678.0,
                                payroll_tax_relief=6000.0)
    assert t.run_invariant('payroll_relief_bounded', [relief_too_big])
    s60e_too_big = YearResult(year=1, employment_income=71_300.0,
                              payroll_pension_contributions=100.0,
                              payroll_s60e_deduction=678.0)
    assert t.run_invariant('payroll_relief_bounded', [s60e_too_big])
    no_job = YearResult(year=1, employment_income=0.0, payroll_ei_premiums=10.0)
    assert t.run_invariant('payroll_relief_bounded', [no_job])
    negative = YearResult(year=1, employment_income=71_300.0,
                          payroll_ei_premiums=-50.0)
    assert t.run_invariant('payroll_relief_bounded', [negative])


# ── Quebec ───────────────────────────────────────────────────────────────

def test_quebec_employee_pays_qpp_reduced_ei_and_qpip_and_nets_differently_from_ontario():
    qc = _run(_single_earner('quebec', _full_year('employment')))[0]
    qc_other = _run(_single_earner('quebec', _full_year('other')))[0]
    on = _run(_single_earner('ontario', _full_year('employment')))[0]

    assert qc.payroll_pension_contributions == pytest.approx(4339.20, abs=0.01)
    assert qc.payroll_ei_premiums == pytest.approx(860.67, abs=0.01)
    assert qc.payroll_qpip_premiums == pytest.approx(352.22, abs=0.01)
    assert qc.after_tax_income != on.after_tax_income
    assert qc_other.after_tax_income - qc.after_tax_income == pytest.approx(
        _premiums(qc) - qc.payroll_tax_relief, abs=0.01)
    # The Quebec relief is smaller per dollar: no provincial credit, and the
    # federal credit is abated.
    assert (qc.payroll_tax_relief / _premiums(qc)
            < on.payroll_tax_relief / _premiums(on))


def test_ontario_never_pays_qpip():
    results = _run(_single_earner('ontario', _full_year('employment'), years=3))
    assert all(r.payroll_qpip_premiums == 0.0 for r in results)
    assert all(r.payroll_ei_premiums > 0 for r in results)


# ── Only employment days pay ─────────────────────────────────────────────

def test_mid_year_job_loss_charges_only_employment_days():
    """Employed Jan 1 - Jul 1 2025, EI after. The premiums equal those of a
    household whose ONLY income is a full-year salary equal to the
    employment slice (181 of 365 days of $71,300). The basic exemption is
    not prorated for a job loss (only for turning 18/70 or death)."""
    job_loss = [
        {"kind": "employment", "amount": YMPE_2025, "from": "2025-01-01", "to": "2025-07-01"},
        {"kind": "ei", "amount": 30_000, "from": "2025-07-01", "to": None},
    ]
    slice_ = YMPE_2025 * 181 / 365
    a = _run(_single_earner('ontario', job_loss))[0]
    b = _run(_single_earner('ontario', _full_year('employment', slice_), gross=slice_))[0]
    for f in PAYROLL_FIELDS[:4]:
        assert getattr(a, f) == pytest.approx(getattr(b, f), abs=0.01), f
    assert a.payroll_ei_premiums > 0
    # Strictly less than a full year at the YMPE.
    assert a.payroll_pension_contributions < 4034.10 - 1000


@pytest.mark.parametrize('config', [ei_only_household_config,
                                    investment_only_household_config])
def test_non_employment_income_pays_no_premium(config):
    for r in _run(config()):
        assert all(getattr(r, f) == 0.0 for f in PAYROLL_FIELDS)


# ── Runway can only shorten ──────────────────────────────────────────────

def test_runway_never_increases():
    from runway import compute_runway
    shock = date(2026, 1, 1)
    emp = compute_runway(_run(runway_household_config('employment')),
                         shock_date=shock, start_year=2025)
    other = compute_runway(_run(runway_household_config('other')),
                           shock_date=shock, start_year=2025)
    assert emp.runway_months < other.runway_months
    assert emp.first_shortfall_year <= other.first_shortfall_year
    # origin/main (594b6f8) reported the counterfactual's figures for the
    # employee too -- the premiums did not exist there.
    assert other.runway_months == pytest.approx(RUNWAY_BASELINE[0])
    assert other.first_shortfall_year == RUNWAY_BASELINE[1]
    assert emp.runway_months < RUNWAY_BASELINE[0]


# ── Retirees / zero employment are byte-identical ────────────────────────

def test_retiree_household_byte_identical():
    results = _run(retiree_household_config())
    assert [repr(r.total_assets) for r in results] == RETIREE_BASELINE_TOTAL_ASSETS
    for r in results:
        assert all(getattr(r, f) == 0.0 for f in PAYROLL_FIELDS)


def test_ei_only_household_byte_identical():
    results = _run(ei_only_household_config())
    assert [repr(r.total_assets) for r in results] == EI_ONLY_BASELINE_TOTAL_ASSETS


# ── Both fold paths agree ────────────────────────────────────────────────

@pytest.mark.parametrize('province', ['ontario', 'quebec'])
def test_annual_and_monthly_paths_agree_on_payroll(province):
    runs = {}
    for ts in ('yearly', 'monthly'):
        for kind in ('employment', 'other'):
            runs[ts, kind] = _run(_single_earner(
                province, _full_year(kind), years=3, time_step=ts))
    for i in range(3):
        y, m = runs['yearly', 'employment'][i], runs['monthly', 'employment'][i]
        for f in PAYROLL_FIELDS:
            assert getattr(y, f) == getattr(m, f), (i, f)
        assert getattr(y, 'payroll_pension_contributions') > 0
        delta_y = runs['yearly', 'other'][i].after_tax_income - y.after_tax_income
        delta_m = runs['monthly', 'other'][i].after_tax_income - m.after_tax_income
        assert delta_y == pytest.approx(delta_m, abs=1e-6)
        assert delta_y > 0


# ── Retirement stops the premiums ────────────────────────────────────────

def test_premiums_stop_at_retirement():
    """The primary retires at 67 (in 2027); the spouse keeps working. From
    2027 on, the household's premiums are the spouse's alone -- the same as a
    one-earner household on the spouse's salary."""
    couple = SimulationConfig(
        projection_years=4, house_value=0, mortgage_balance=0,
        margin_available=0, start_year=2025, province='ontario',
        savings_rate=0.0, living_costs=LIVING_COSTS,
        family_members=[
            {"role": "primary", "birth_year": 1960, "retirement_age": 67,
             "gross_income": 90_000, "rrsp_room_accumulated": 0,
             "tfsa_room_accumulated": 0},
            {"role": "spouse", "birth_year": 1975, "retirement_age": 65,
             "gross_income": 60_000, "rrsp_room_accumulated": 0,
             "tfsa_room_accumulated": 0},
        ])
    spouse_alone = _single_earner('ontario', None, gross=60_000, years=4,
                                  extra={"birth_year": 1975, "retirement_age": 65})
    rc, rs = _run(couple), _run(spouse_alone)
    # 2025-2026: both earn -> more than the spouse alone.
    for i in (0, 1):
        assert _premiums(rc[i]) > _premiums(rs[i]) + 1000
    # 2027-2028: the primary is retired -> exactly the spouse's premiums.
    for i in (2, 3):
        for f in PAYROLL_FIELDS[:4]:
            assert getattr(rc[i], f) == pytest.approx(getattr(rs[i], f), abs=0.01), (i, f)
        assert _premiums(rc[i]) > 0


# ── Per-member ceilings, never pooled ────────────────────────────────────

def test_two_earners_each_pay_their_own_maximum():
    couple = SimulationConfig(
        projection_years=1, house_value=0, mortgage_balance=0,
        margin_available=0, start_year=2025, province='ontario',
        savings_rate=0.0, living_costs=LIVING_COSTS,
        family_members=[
            {"role": role, "birth_year": 1985, "gross_income": YMPE_2025,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0}
            for role in ('primary', 'spouse')])
    r = _run(couple)[0]
    one = _run(_single_earner('ontario', None))[0]
    assert r.payroll_pension_contributions == pytest.approx(2 * 4034.10, abs=0.01)
    assert r.payroll_ei_premiums == pytest.approx(2 * 1077.48, abs=0.01)
    for f in PAYROLL_FIELDS:
        assert getattr(r, f) == pytest.approx(2 * getattr(one, f), abs=0.01), f


# ── The relief reaches the tax consumers ─────────────────────────────────

def test_tuition_credit_floors_against_post_payroll_relief_tax():
    """A low-income Ontario employee whose federal tuition credit exceeds
    their whole tax bill: the non-refundable tuition credit wipes out
    whatever tax is left, so the tax the household pays is zero in both the
    employee run and the ``'other'`` counterfactual. The employee's
    after-tax income is therefore lower by the FULL premiums -- the s.118.7
    credit and s.60(e) deduction only shrink the tax the tuition credit
    absorbs (the rest of the tuition credit carries forward). If the payroll
    relief were applied after the tuition rule read ``tax_before``, the
    employee would keep the relief on top of a fully-used tuition credit and
    the gap would be premiums minus relief."""
    tuition = {"tuition_by_year": {2025: 60_000}}
    emp = _run(_single_earner('ontario', _full_year('employment', 30_000),
                              gross=30_000, extra=tuition))[0]
    other = _run(_single_earner('ontario', _full_year('other', 30_000),
                                gross=30_000, extra=tuition))[0]
    assert emp.payroll_tax_relief > 100
    assert other.after_tax_income == pytest.approx(30_000, abs=0.01)
    assert other.after_tax_income - emp.after_tax_income == pytest.approx(
        _premiums(emp), abs=0.01)


# ── Extra accumulating adults (#899) ─────────────────────────────────────

def _three_adult_run(kind):
    """The #899 three-adult contract (primary couple + 'ac', an accumulating
    adult child on $60,000), with ac's year declared as ONE full-year income
    segment of ``kind`` -- the engine-level counterfactual (the contract maps
    only employment into gross_income, so the kind is set on the mapped
    member)."""
    import input_contract as ic
    from test_issue_899_nadult_accumulators import (
        _add_accumulator_adult, _load_example, _two_generation_subset)
    doc = _add_accumulator_adult(_two_generation_subset(_load_example()))
    cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
    ac = next(m for m in cfg.family_members if m.get('role') == 'ac')
    ac['income_segments'] = _full_year(kind, ac['gross_income'],
                                       f"{cfg.start_year}-01-01")
    return cfg, _run(cfg)


def test_extra_adult_savings_net_of_premiums():
    """The third adult ('ac', $60,000 employment in Quebec) invests only what
    is left after tax AND premiums: vs the same gross as ``'other'``-kind
    income, their year-0 savings drop by savings_rate x (premiums - relief),
    and that net cost equals the engine's own figure for a one-earner Quebec
    household on the same salary. Their premiums never enter the primary
    couple's cash (INV-22)."""
    cfg, emp = _three_adult_run('employment')
    _, other = _three_adult_run('other')
    ac_emp = emp[0].extra_adult_accounts[0]
    ac_other = other[0].extra_adult_accounts[0]
    assert ac_emp['id'] == 'ac'

    solo = _run(_single_earner(cfg.province, _full_year('employment', 60_000, f"{cfg.start_year}-01-01"),
                               gross=60_000, start_year=cfg.start_year))[0]
    net_cost = _premiums(solo) - solo.payroll_tax_relief
    assert net_cost > 1000
    assert ac_other['savings'] - ac_emp['savings'] == pytest.approx(
        cfg.savings_rate * net_cost, abs=0.01)
    # The primary couple's premiums and cash are unaffected by ac's kind.
    for f in PAYROLL_FIELDS:
        assert getattr(emp[0], f) == getattr(other[0], f), f
    assert emp[0].after_tax_income == other[0].after_tax_income
