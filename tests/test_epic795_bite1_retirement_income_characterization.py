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
taxed RRIF cash is reinvested). Years 1-21 are still the original capture.

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
        (1, 0.0, 0.0, 0.0, 0.0, 0.0, 493228.9341344428),
        (2, 0.0, 0.0, 0.0, 0.0, 0.0, 557656.5193486448),
        (3, 0.0, 0.0, 0.0, 0.0, 0.0, 626541.1124799167),
        (4, 0.0, 0.0, 0.0, 0.0, 0.0, 700156.4239757287),
        (5, 0.0, 0.0, 0.0, 0.0, 0.0, 778688.3826508056),
        (6, 0.0, 0.0, 0.0, 0.0, 0.0, 862517.5059368013),
        (7, 0.0, 0.0, 0.0, 0.0, 0.0, 951965.8996059925),
        (8, 0.0, 0.0, 0.0, 0.0, 0.0, 1047374.701204929),
        (9, 0.0, 0.0, 0.0, 0.0, 0.0, 1149105.196623022),
        (10, 0.0, 0.0, 0.0, 0.0, 0.0, 1257540.0021535621),
        (11, 0.0, 0.0, 0.0, 0.0, 0.0, 1372989.4740392992),
        (12, 0.0, 0.0, 0.0, 0.0, 0.0, 1495972.688510753),
        (13, 0.0, 0.0, 0.0, 0.0, 0.0, 1626944.02643619),
        (14, 0.0, 0.0, 0.0, 0.0, 0.0, 1766384.6835873474),
        (15, 0.0, 0.0, 0.0, 0.0, 0.0, 1914804.2455112848),
        (16, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2046781.3604760636),
        (17, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2186967.4160924368),
        (18, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2335857.805764484),
        (19, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2493977.534618322),
        (20, 24000.0, 17816.0, 9000.0, 11184.0, 11184.0, 2631261.3267317326),
        (21, 24000.0, 17816.0, 9000.0, 11184.0, 11184.0, 2776715.0457589757),
        # Years 22-28: forced RRIF minimum priced progressively + OAS clawback
        # (#825), and #1001 nets the RRIF after-tax into the discretionary
        # drawdown target -- the mandatory minimum's after-tax proceeds fund
        # the net spending shortfall first, so drawdown_net_target drops to 0
        # (the RRIF covers it) and drawdown_income is the RRIF gross only (no
        # discretionary TFSA draw). total_assets rises (TFSA preserved).
        (22, 24000.0, 17816.0, 9000.0, 55729.29382836383, 0.0, 2914275.209826546),
        (23, 24000.0, 17816.0, 9000.0, 57406.23894265369, 0.0, 3059332.918670074),
        (24, 24000.0, 17816.0, 9000.0, 59140.97043722077, 0.0, 3212305.6531399493),
        (25, 24000.0, 17816.0, 9000.0, 60923.209783042184, 0.0, 3373638.897183636),
        (26, 24000.0, 17816.0, 9000.0, 114042.859639776, 0.0, 3530046.775695531),
        (27, 24000.0, 17760.92209999802, 9000.0, 117427.45600649116, 0.0, 3694314.581516667),
        (28, 24000.0, 17483.5372912493, 9000.0, 120873.59383512283, 0.0, 3866868.554041964),
    ],
    'monthly': [
        (1, 0.0, 0.0, 0.0, 0.0, 0.0, 494009.7877761803),
        (2, 0.0, 0.0, 0.0, 0.0, 0.0, 559368.3685158564),
        (3, 0.0, 0.0, 0.0, 0.0, 0.0, 629350.2460392427),
        (4, 0.0, 0.0, 0.0, 0.0, 0.0, 704246.6855982656),
        (5, 0.0, 0.0, 0.0, 0.0, 0.0, 784262.3466774243),
        (6, 0.0, 0.0, 0.0, 0.0, 0.0, 869798.4142966167),
        (7, 0.0, 0.0, 0.0, 0.0, 0.0, 961199.3451176765),
        (8, 0.0, 0.0, 0.0, 0.0, 0.0, 1058830.5118554474),
        (9, 0.0, 0.0, 0.0, 0.0, 0.0, 1163079.4651536278),
        (10, 0.0, 0.0, 0.0, 0.0, 0.0, 1274357.2715856596),
        (11, 0.0, 0.0, 0.0, 0.0, 0.0, 1393004.1733508448),
        (12, 0.0, 0.0, 0.0, 0.0, 0.0, 1519573.0846602616),
        (13, 0.0, 0.0, 0.0, 0.0, 0.0, 1654554.4060504613),
        (14, 0.0, 0.0, 0.0, 0.0, 0.0, 1798468.2822341078),
        (15, 0.0, 0.0, 0.0, 0.0, 0.0, 1951866.3984413184),
        (16, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2089358.3935415805),
        (17, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2235619.271746401),
        (18, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2391192.5007570554),
        (19, 13200.0, 8908.0, 9000.0, 0.0, 0.0, 2556654.9347280017),
        (20, 24000.0, 17816.0, 9000.0, 11184.0, 11184.0, 2701966.40467288),
        (21, 24000.0, 17816.0, 9000.0, 11184.0, 11184.0, 2856169.9727510232),
        # Years 22-28: #825 progressive RRIF pricing + #1001 RRIF-after-tax
        # nets the discretionary target (drawdown_net_target -> 0, the RRIF
        # covers the net shortfall; drawdown_income is the RRIF gross only).
        (22, 24000.0, 17816.0, 9000.0, 57488.80864633473, 0.0, 3002624.5629486344),
        (23, 24000.0, 17816.0, 9000.0, 59317.346734579456, 0.0, 3157294.373365883),
        (24, 24000.0, 17816.0, 9000.0, 61211.74835592436, 0.0, 3320649.8882078663),
        (25, 24000.0, 17816.0, 9000.0, 63161.693354907235, 0.0, 3493193.4917558106),
        (26, 24000.0, 17674.945655551273, 9000.0, 118245.61464149998, 0.0, 3661054.355025599),
        (27, 24000.0, 17371.339610248433, 9000.0, 121958.2887978903, 0.0, 3837611.1842350233),
        (28, 24000.0, 17065.41178435127, 9000.0, 125747.26570545296, 0.0, 4023347.7265667515),
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
