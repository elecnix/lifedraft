#!/usr/bin/env python3
"""Epic #795 bite 1 characterization test (DP#26 -- behavior-preserving refactor).

The retirement transition was extracted from the fold's prologue (two inline
call sites: ``simulate_year`` and ``_run_monthly``) into a single registered
``retirement_income`` rule. This test guards the case the golden terminal
invariant cannot: a fabricated household that RETIRES mid-projection must
reproduce its full year-by-year trajectory (CPP, OAS, pension, drawdown,
drawdown_net_target, total_assets) byte-for-byte against the origin/main
baseline captured BEFORE the refactor -- for BOTH the yearly and the monthly
time-step folds (the monthly path is the second spelling the bite unifies).

The baseline below was captured on origin/main (commit 2ebaa05) by running
this exact fabricated household. It is embedded here as a regression pin: if
the extraction changes any number in any retirement year, this file goes red
with the year + field that moved. Round numbers, role-based names (DP#4/DP#15)
-- no real data.

Issue #825 INTENTIONALLY moved the forced-RRIF-minimum years (22-28): the
mandatory minimum's tax is now priced through the same progressive re-bracketing
+ per-spouse OAS-clawback machinery as the discretionary drawdown, not a flat
placeholder rate. The forced GROSS (``drawdown_income``) is unchanged; only the
after-tax reinvestment (``total_assets``) and, in the highest-income years, the
recovered ``oas_income`` move. Those years were repinned to the post-#825 engine;
every pre-forced-RRIF year is still the original origin/main capture.

Issue #1001 INTENTIONALLY moved the same years (22-28) again: the forced RRIF
minimum's after-tax proceeds now fund the net spending shortfall BEFORE the
discretionary drawdown is sized, so ``drawdown_net_target`` drops to 0 (the RRIF
covers it) and ``drawdown_income`` is the RRIF gross only (no discretionary TFSA
draw). ``total_assets`` rises (tax-free TFSA is preserved instead of drawn while
taxed RRIF cash is reinvested).

Issue #384 INTENTIONALLY repinned EVERY year of both baselines: this household
declares no contribution strategy, so the fold fell back to the implicit
default -- which was the Smith Manoeuvre (55% non-reg, deduct-later). #384
made the fallback the neutral no-readvance baseline (DP#13: a default is a
fallback for absent input, never an opinion), so the accumulation allocation,
the drawdown sizing and every terminal balance move from year 1. The baseline
was re-captured from a live run of THIS engine, not by hand; every year is
re-pinned, none is left from the original origin/main capture.

Run: uv run pytest tests/test_epic795_bite1_retirement_income_characterization.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter


def _char_household_config(time_step='yearly'):
    """A fabricated couple that retires mid-projection (primary born 1976,
    retirement_age 65 -> retires 2041 = projection year 16; spouse born 1980
    -> retires 2045 = year 20). Carries RRSP/TFSA balances + a pension so the
    drawdown the rule sizes and the RRIF minimum (age 71, year 26) both
    exercise the rule's full output path. Ontario, frozen brackets."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1976, 'gross_income': 110_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_100,
                 'rrsp_room_accumulated': 35_000, 'tfsa_room_accumulated': 18_000,
                 'rrsp_balance': 250_000, 'tfsa_balance': 35_000,
                 'pension_income_annual': 9_000},
                {'role': 'spouse', 'birth_year': 1980, 'gross_income': 70_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 900,
                 'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 18_000,
                 'rrsp_balance': 120_000, 'tfsa_balance': 28_000},
            ],
            'children': [],
        },
        'accounts': {'resp_current_balance': 0, 'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': 2026, 'projection_years': 28,
            'investment_return': 0.06, 'salary_growth': 0.02,
            'inflation': 0.02, 'frozen_brackets': True, 'time_step': time_step,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {'balance': 0, 'cost_basis': 0,
                            'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                            'yield': {'eligible_dividends': 0.015, 'interest': 0.01}},
            },
        },
        'property': {
            'house_value': 750_000, 'mortgage_balance': 350_000,
            'mortgage_rate': 0.045, 'amortization_years': 20,
            'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False,
        },
        'savings': {'rate': 0.18},
        'retirement': {'spending_target': 62_000, 'rrif_conversion_age': 71,
                       'drawdown_order': ['tfsa', 'non_reg', 'rrsp']},
        'tax': {'province': 'ontario'},
    }


def _run_char(time_step):
    cfg = SimulationConfig.from_dict(_char_household_config(time_step))
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


# Captured on origin/main (2ebaa05) BEFORE the refactor. Each tuple is
# (year, cpp_income, oas_income, pension_income, drawdown_income,
#  drawdown_net_target, total_assets) for one projected year.
CHARACTERIZATION_BASELINE = {
    'yearly': [
        (1, 0.0, 0.0, 0.0, 0.0, 0.0, 493286.69567300926),
        (2, 0.0, 0.0, 0.0, 0.0, 0.0, 557837.4615165568),
        (3, 0.0, 0.0, 0.0, 0.0, 0.0, 626919.0148516129),
        (4, 0.0, 0.0, 0.0, 0.0, 0.0, 700814.1849788303),
        (5, 0.0, 0.0, 0.0, 0.0, 0.0, 779782.0509545027),
        (6, 0.0, 0.0, 0.0, 0.0, 0.0, 864169.3902545602),
        (7, 0.0, 0.0, 0.0, 0.0, 0.0, 954312.0841104442),
        (8, 0.0, 0.0, 0.0, 0.0, 0.0, 1050566.23173489),
        (9, 0.0, 0.0, 0.0, 0.0, 0.0, 1153309.3570720789),
        (10, 0.0, 0.0, 0.0, 0.0, 0.0, 1262941.6874358037),
        (11, 0.0, 0.0, 0.0, 0.0, 0.0, 1379871.1244404276),
        (12, 0.0, 0.0, 0.0, 0.0, 0.0, 1504585.473811145),
        (13, 0.0, 0.0, 0.0, 0.0, 0.0, 1637564.4838653689),
        (14, 0.0, 0.0, 0.0, 0.0, 0.0, 1779316.8027166522),
        (15, 0.0, 0.0, 0.0, 0.0, 0.0, 1930381.7075652597),
        (16, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2063868.2479220503),
        (17, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2205698.406159383),
        (18, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2356378.407742647),
        (19, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2516444.901932792),
        (20, 24000, 17816.0, 9000, 11184.0, 11184.0, 2655833.27144398),
        (21, 24000, 17816.0, 9000, 11184.0, 11184.0, 2803561.1986680795),
        (22, 24000, 16917.845065513095, 9000, 70202.69956324603, 0.0, 2938350.0701185595),
        (23, 24000, 16600.975607938988, 9000, 72315.16261374009, 0.0, 3080336.4861147963),
        (24, 24000, 16273.18704584704, 9000, 74500.41969435307, 0.0, 3229915.109731785),
        (25, 24000, 15936.421572746383, 9000, 76745.52284835745, 0.0, 3387508.2110297186),
        (26, 24000, 15592.88181245328, 9000, 125666.67642166922, 0.0, 3541434.160207511),
        (27, 24000, 15245.034717405475, 9000, 129388.82288214425, 0.0, 3703042.760757488),
        (28, 24000, 14895.61054468998, 9000, 133169.8353272506, 0.0, 3872692.503839247),
    ],
    'monthly': [
        (1, 0.0, 0.0, 0.0, 0.0, 0.0, 494067.5493147467),
        (2, 0.0, 0.0, 0.0, 0.0, 0.0, 559549.5045097579),
        (3, 0.0, 0.0, 0.0, 0.0, 0.0, 629728.960808238),
        (4, 0.0, 0.0, 0.0, 0.0, 0.0, 704906.5748704649),
        (5, 0.0, 0.0, 0.0, 0.0, 0.0, 785360.6838048605),
        (6, 0.0, 0.0, 0.0, 0.0, 0.0, 871459.1302067677),
        (7, 0.0, 0.0, 0.0, 0.0, 0.0, 963560.6515843219),
        (8, 0.0, 0.0, 0.0, 0.0, 0.0, 1062046.1633639818),
        (9, 0.0, 0.0, 0.0, 0.0, 0.0, 1167320.1193375194),
        (10, 0.0, 0.0, 0.0, 0.0, 0.0, 1279811.9554218596),
        (11, 0.0, 0.0, 0.0, 0.0, 0.0, 1399960.8780527571),
        (12, 0.0, 0.0, 0.0, 0.0, 0.0, 1528289.271375433),
        (13, 0.0, 0.0, 0.0, 0.0, 0.0, 1665314.1132922932),
        (14, 0.0, 0.0, 0.0, 0.0, 0.0, 1811584.3661837506),
        (15, 0.0, 0.0, 0.0, 0.0, 0.0, 1967682.9436951485),
        (16, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2106732.8150835442),
        (17, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2254693.178504504),
        (18, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2412119.1444464885),
        (19, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2579600.1039935146),
        (20, 24000, 17816.0, 9000, 11184.0, 11184.0, 2727097.529174951),
        (21, 24000, 17816.0, 9000, 11184.0, 11184.0, 2883667.3412254485),
        (22, 24000, 16605.10547578882, 9000, 72287.63016140787, 0.0, 3027336.93137152),
        (23, 24000, 16260.219171380655, 9000, 74586.87219079564, 0.0, 3178905.4953115713),
        (24, 24000, 15902.910139400003, 9000, 76968.93240399998, 0.0, 3338818.5854336373),
        (25, 24000, 15535.124896739873, 9000, 79420.83402173418, 0.0, 3507553.0880359104),
        (26, 24000, 15159.092744148023, 9000, 130205.42682678421, 0.0, 3672950.5093933814),
        (27, 24000, 14777.332153967156, 9000, 134286.05951208738, 0.0, 3846795.7526906575),
        (28, 24000, 14392.65210831539, 9000, 138441.46583679976, 0.0, 4029531.47886477),
    ],
}


_FIELDS = ('year', 'cpp_income', 'oas_income', 'pension_income',
           'drawdown_income', 'drawdown_net_target', 'total_assets')


@pytest.mark.parametrize('time_step', ['yearly', 'monthly'])
def test_retiring_household_trajectory_matches_origin_main(time_step):
    """Both time-step folds reproduce the origin/main trajectory for a
    household that retires mid-projection -- CPP/OAS/pension onset, drawdown
    sizing, RRIF minimum at 71, and total_assets, every year."""
    results = _run_char(time_step)
    baseline = CHARACTERIZATION_BASELINE[time_step]
    assert len(results) == len(baseline), (
        f"{time_step}: projected {len(results)} years, baseline has "
        f"{len(baseline)} -- the household's projection_years changed")
    for res, base in zip(results, baseline):
        actual = (res.year, res.cpp_income, res.oas_income, res.pension_income,
                  res.drawdown_income, res.drawdown_net_target, res.total_assets)
        for i, (got, exp) in enumerate(zip(actual, base)):
            if isinstance(exp, float):
                assert got == pytest.approx(exp), (
                    f"{time_step} year {res.year} {_FIELDS[i]}: got {got!r}, "
                    f"expected {exp!r} (origin/main baseline moved -- the "
                    f"retirement_income extraction changed a number)")
            else:
                assert got == exp, (
                    f"{time_step} year {res.year} {_FIELDS[i]}: got {got!r}, "
                    f"expected {exp!r}")


def test_characterization_household_actually_retires():
    """Guard on the test's own premise: the household must reach retirement
    within the projection (else the trajectory pin is vacuous)."""
    results = _run_char('yearly')
    retired_years = [r for r in results if r.any_retired]
    assert retired_years, "the characterization household never retired -- the pin is vacuous"
    # Primary retires at 2041 (year 16); CPP/OAS must be nonzero from then.
    assert any(r.cpp_income > 0 for r in retired_years)
    assert any(r.oas_income > 0 for r in retired_years)
