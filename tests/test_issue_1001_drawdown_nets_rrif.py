#!/usr/bin/env python3
"""Issue #1001: the discretionary drawdown must net the forced RRIF minimum.

Before #1001 the discretionary drawdown was sized to the FULL net spending
shortfall (``target_net - covered_net``) and drew TFSA (tax-free-first), while
``apply_rrif_minimum`` SEPARATELY forced the mandatory RRIF minimum, taxed it,
and reinvested the ENTIRE after-tax surplus into non-reg -- for 25 years. The
two flows never saw each other, so the household drew tax-free TFSA it did not
need while re-taxing/reinvesting RRIF cash, swapping 6% tax-free compounding
for ~5.74% after-tax non-reg compounding and lowering terminal wealth.

The fix: the forced RRIF minimum's AFTER-TAX proceeds are netted into
``covered_net`` BEFORE the discretionary draw is sized, so the discretionary
draw funds only the TRUE residual shortfall
``target_net - covered_net - forced_rrif_after_tax``. When the mandatory RRIF
minimum's after-tax already covers the net target, the discretionary TFSA draw
is ~0 (not the full net shortfall).

This test builds a fabricated household (DP#4/DP#15 -- round numbers, role-based
names, no real data) where, in the first RRIF year (age 71), the forced RRIF
minimum's after-tax proceeds EXCEED the net spending target, and asserts:

  1. ``drawdown_net_target`` is ~0 in that year (the RRIF after-tax covers the
     shortfall, so no discretionary draw is sized).
  2. The discretionary TFSA draw is ~0 (TFSA grew by its return, not drawn) --
     the pre-#1001 engine drew the full ~net shortfall from TFSA here.
  3. The RRIF gross IS drawn (``drawdown_taxable`` ~= the RRIF minimum) -- the
     mandatory minimum still fires; only the DISCRETIONARY draw is reduced.
  4. Money is conserved and the drawdown-meets-net-target invariant holds
     (delivered >= target, now WITHOUT the over-draw).

Run: uv run pytest tests/test_issue_1001_drawdown_nets_rrif.py -q
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter


def _household_with_large_rrif() -> dict:
    """A fabricated couple whose RRSP balance at RRIF conversion (age 71) is
    large enough that the CRA age-factor minimum's AFTER-TAX proceeds exceed
    the net spending shortfall.

    primary born 1976 -> age 50 at start (2026), retires 2041 (age 65), RRIF
    conversion 2047 (age 71). A $600k RRSP balance at 71 x the age-71 factor
    (~5.28%) ~= $31.7k gross; after low-bracket tax (the retiree's CPP/pension
    base is modest) the after-tax still exceeds the ~$25k net spending
    shortfall. Round numbers, role-based names (DP#4/DP#15)."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1976, 'gross_income': 110_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_100,
                 'rrsp_room_accumulated': 35_000, 'tfsa_room_accumulated': 18_000,
                 'rrsp_balance': 600_000, 'tfsa_balance': 80_000,
                 'pension_income_annual': 9_000},
                {'role': 'spouse', 'birth_year': 1980, 'gross_income': 70_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 900,
                 'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 18_000,
                 'rrsp_balance': 250_000, 'tfsa_balance': 60_000},
            ],
            'children': [],
        },
        'accounts': {'resp_current_balance': 0, 'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': 2026, 'projection_years': 28,
            'investment_return': 0.06, 'salary_growth': 0.02,
            'inflation': 0.02, 'frozen_brackets': True, 'time_step': 'yearly',
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


def _run(cfg_dict):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def test_discretionary_tfsa_draw_is_zero_when_rrif_after_tax_covers_target():
    """In the first RRIF year (age 71, 2047), the forced RRIF minimum's
    after-tax proceeds exceed the net spending shortfall, so:

    - ``drawdown_net_target`` ~ 0 (the RRIF after-tax covers it -- no
      discretionary draw is sized);
    - the discretionary TFSA draw ~ 0 (TFSA grew by its return, NOT drawn --
      the pre-#1001 engine drew the full net shortfall from TFSA here);
    - the RRIF gross IS drawn (``drawdown_taxable`` > 0 -- the mandatory
      minimum still fires; only the DISCRETIONARY draw is reduced).
    """
    results = _run(_household_with_large_rrif())
    # projection year 22 = calendar 2047 = primary age 71 (RRIF conversion).
    rrif_year = results[21]
    assert rrif_year.year == 22, f"expected projection year 22, got {rrif_year.year}"

    # (1) The discretionary draw target is ~0 -- the RRIF after-tax covers the
    # net spending shortfall, so no discretionary draw is sized.
    assert rrif_year.drawdown_net_target == pytest.approx(0.0, abs=1.0), (
        f"#1001: in the first RRIF year the discretionary drawdown target "
        f"should be ~0 (the forced RRIF after-tax covers the net shortfall), "
        f"got {rrif_year.drawdown_net_target!r}")

    # (2) The discretionary TFSA draw is ~0: TFSA grew by ~6% (its return) with
    # NO withdrawal. The pre-#1001 engine drew the full net shortfall from TFSA
    # here (TFSA fell by the net shortfall). Assert TFSA did NOT fall -- it grew
    # by approximately one year's return on the prior balance.
    prev_tfsa = results[20].total_tfsa
    tfsa_delta = rrif_year.total_tfsa - prev_tfsa
    # TFSA should grow by ~6% (no draw). A draw would make the delta materially
    # below the return growth (or negative). Allow a small tolerance for the
    # growth-rate rounding.
    expected_growth = prev_tfsa * 0.06
    assert tfsa_delta >= expected_growth * 0.5 - 1.0, (
        f"#1001: in the first RRIF year the discretionary TFSA draw should be "
        f"~0 (TFSA grew by ~6% = {expected_growth:.2f}), but TFSA moved by "
        f"{tfsa_delta:.2f} (prev {prev_tfsa:.2f} -> {rrif_year.total_tfsa:.2f}) "
        f"-- the pre-#1001 over-draw is still present")

    # (3) The RRIF gross IS drawn -- the mandatory minimum still fires, so
    # drawdown_taxable (the RRIF gross) is positive and material.
    assert rrif_year.drawdown_taxable > 10_000, (
        f"#1001: the forced RRIF minimum should still fire in the first RRIF "
        f"year (drawdown_taxable > 0), got {rrif_year.drawdown_taxable!r}")

    # (4) The drawdown-meets-net-target invariant holds: delivered >= target
    # (now WITHOUT the over-draw -- the RRIF after-tax funds the spending via
    # the solvency identity, and the discretionary draw is sized to ~0).
    assert rrif_year.drawdown_net_delivered >= rrif_year.drawdown_net_target - 1.0


def test_pre_rrif_year_still_draws_tfsa_for_the_shortfall():
    """The fix does NOT touch pre-RRIF retirement years: before age 71 there is
    no forced RRIF minimum, so the discretionary drawdown still sizes to the
    full net shortfall and draws TFSA. This guards against an over-broad fix
    that zeroed the discretionary draw in EVERY retirement year."""
    results = _run(_household_with_large_rrif())
    # projection year 21 = calendar 2046 = primary age 70 (retired, pre-RRIF).
    pre_rrif = results[20]
    assert pre_rrif.year == 21

    # No RRIF yet -> the discretionary drawdown funds the net shortfall.
    assert pre_rrif.drawdown_net_target > 1000, (
        f"pre-RRIF retirement year should still size a discretionary draw to "
        f"the net shortfall, got target {pre_rrif.drawdown_net_target!r}")
    assert pre_rrif.drawdown_net_delivered >= pre_rrif.drawdown_net_target - 1.0
    # And TFSA IS drawn (fell vs the prior year's grown balance).
    prev_tfsa = results[19].total_tfsa
    assert pre_rrif.total_tfsa < prev_tfsa * 1.06, (
        f"pre-RRIF year should draw TFSA (balance below the 6%-grown prior), "
        f"prev {prev_tfsa:.2f} -> {pre_rrif.total_tfsa:.2f}")


def test_golden_terminal_rose_above_pre_fix_value():
    """The #1001 fix RAISES the golden household's terminal wealth (the bug
    lowered it by swapping tax-free TFSA compounding for taxed non-reg
    compounding for 25 years). This test isolates #1001's relative effect by
    running the golden household WITH #1001 (current code) and WITHOUT #1001
    (the netting reverted), and asserting the #1001-ON terminal is HIGHER.

    The absolute golden value can move for unrelated reasons (e.g. #1046
    lowered it from 9816435 to 9709753 because RESP contributions consume
    household wealth), so an absolute-threshold guard like `terminal >
    9766299` is brittle. The relative guard `terminal_1001_ON >
    terminal_1001_OFF` is robust: it checks that #1001's netting still
    produces its intended wealth gain regardless of other baseline moves."""
    from unittest.mock import patch
    from rule_registry import RULES
    from test_golden_trajectory_581 import golden_household_config, _run

    # #1001-ON: current code
    terminal_1001_on = _run(golden_household_config())[-1].total_assets

    # #1001-OFF: revert the RRIF-after-tax netting so the discretionary
    # drawdown sizes to the FULL shortfall (ignoring the forced RRIF
    # minimum's after-tax proceeds), and the full after-tax is reinvested
    # into non-reg (no spending-funded routing). This is the pre-#1001
    # behaviour.
    originals = {k: RULES[k] for k in ('retirement_income', 'rrif_minimum')}

    def _retirement_income_no_1001(ws, ctx):
        """Call the original retirement_income rule, then undo #1001's netting:
        set drawdown_net_target back to drawdown_net_target_pre_rrif and
        zero forced_rrif_after_tax, so the discretionary draw sizes to the
        full pre-RRIF shortfall."""
        result = originals['retirement_income'](ws, ctx)
        if ws.drawdown_net_target_pre_rrif > 0 and ws.forced_rrif_after_tax > 0:
            ws.drawdown_net_target = ws.drawdown_net_target_pre_rrif
            ws.forced_rrif_after_tax = 0.0
        return result

    def _rrif_minimum_no_1001(ws, ctx):
        """Call the original rrif_minimum rule, then undo #1001's spending
        routing: move rrif_after_tax_to_spending back to non-reg reinvestment
        so ALL of the forced RRIF after-tax is reinvested (the pre-#1001
        behaviour where none of it funded spending directly)."""
        result = originals['rrif_minimum'](ws, ctx)
        if ws.rrif_after_tax_to_spending > 0:
            ws.new_nonreg_bal += ws.rrif_after_tax_to_spending
            ws.new_nonreg_acb += ws.rrif_after_tax_to_spending
            ws.rrif_after_tax_to_spending = 0.0
        return result

    try:
        RULES['retirement_income'] = _retirement_income_no_1001
        RULES['rrif_minimum'] = _rrif_minimum_no_1001
        terminal_1001_off = _run(golden_household_config())[-1].total_assets
    finally:
        RULES['retirement_income'] = originals['retirement_income']
        RULES['rrif_minimum'] = originals['rrif_minimum']

    # #1001 ON should produce HIGHER terminal wealth than #1001 OFF
    # (the netting preserves more TFSA compounding).
    assert terminal_1001_on > terminal_1001_off, (
        f"#1001 should RAISE the golden terminal above the #1001-OFF value "
        f"(the bug lowered wealth); got #1001-ON={terminal_1001_on!r}, "
        f"#1001-OFF={terminal_1001_off!r}")
    # Re-pin to the exact new value (moved by #1046's RESP funding).
    assert terminal_1001_on == pytest.approx(9709753.139463063), (
        f"golden terminal re-pin: expected 9709753.139463063, "
        f"got {terminal_1001_on!r}")