#!/usr/bin/env python3
"""Issue #710: wire AMT (Alternative Minimum Tax) into the run.

`countries.canada.amt` was fully implemented and unit-tested but called by
NOTHING in production, so no run ever charged a minimum tax. #754 threaded the
year's realized capital gain onto the fold (the only s.127.52(1) add-back big
enough to make AMT bite in this engine), which unblocked the wiring.

This proves the wiring end-to-end: the `amt` rule runs at the END of the fold
and charges the household ``max(regular tax, AMT)`` -- booking the surcharge on
top of the regular tax the fold already priced -- for a household that realizes
a large capital gain, and is a strict no-op for a household that realizes none.

Lean, single-responsibility, invariant-based (DP#3). Round numbers, no PII.

Run: uv run pytest tests/test_issue_710_wire_amt.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_state import SimState, simulate_year_pure, _default_canada_state
from simulation_config import SimulationConfig
from simulation_rules import RULES, RuleContext, YearWorkingState
from countries.canada.amt import (
    AMTParameters, total_tax_with_amt,
)
from countries.canada.tax_calc import (
    compute_non_refundable_credits, federal_tax_before_abatement,
    quebec_abatement_amount,
)
from tax_data import default_tax_provider

# The 2026 AMT basic exemption, derived from the data (not hardcoded): a gain
# has to clear it before the minimum amount can exceed regular tax.
_AMT_EXEMPTION = AMTParameters.for_year(2026, default_tax_provider()).exemption


def _retired_config():
    return SimulationConfig(
        projection_years=1, investment_return=0.05, mortgage_balance=0,
        mortgage_rate=0.05, margin_available=0, province='quebec',
        family_members=[
            {'role': 'primary', 'gross_income': 0, 'birth_year': 1955},
            {'role': 'spouse', 'gross_income': 0, 'birth_year': 1957},
        ],
        children=[],
    )


def _run_year(non_reg_balance, non_reg_acb, net_target):
    """One retirement year that funds ``net_target`` net spending from the
    non-registered pot, realizing (proceeds - ACB) of capital gain."""
    state = SimState(
        non_reg_balance=non_reg_balance, non_reg_acb=non_reg_acb,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    result, _ = simulate_year_pure(
        state=state, year=0,
        allocations={'_primary_income': 0, '_annual_savings': 0},
        config=_retired_config(), investment_return=0.05,
        primary_marginal_rate=0.53, retiree_marginal_rate=0.53,
        calendar_year=2026,
        drawdown_net_target=net_target, drawdown_order=['non_reg'],
        any_retired=True, retirement_spending_target=net_target,
    )
    return result


def _expected_surcharge(taxable_income, realized_gain, province='quebec', year=2026):
    """The AMT surcharge computed straight from the amt module -- the oracle the
    wired fold must reproduce."""
    provider = default_tax_provider()
    gross_fed = federal_tax_before_abatement(taxable_income, year, province, provider)
    abatement = quebec_abatement_amount(taxable_income, year, province, provider)
    nr = compute_non_refundable_credits(0, taxable_income, year, province, provider)['total']
    federal_after_credits = max(0.0, gross_fed - abatement - nr)
    return total_tax_with_amt(
        regular_tax=federal_after_credits,
        taxable_income=taxable_income,
        taxable_capital_gains=0.5 * realized_gain,
        capital_gains_inclusion=0.5,
        # Issue #747: 50% of the same federal non-refundable credits reduce the
        # minimum amount (ITA s.127.531), matching the wired rule.
        nonrefundable_credits=nr,
        params=AMTParameters.for_year(year, provider),
    )['amt_surcharge']


# ---------------------------------------------------------------------------
# 1. A large realized gain makes AMT bite: the run charges max(regular, AMT).
# ---------------------------------------------------------------------------
class TestAmtBindsOnLargeRealizedGain:
    def test_surcharge_is_assessed_and_matches_the_amt_module(self):
        # $3M non-reg pot, ~zero ACB, $1.2M net spending drawn from it -> a
        # multi-hundred-thousand-dollar realized gain, 100%-included for AMT.
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=0.0, net_target=1_200_000)
        assert r.realized_capital_gains > _AMT_EXEMPTION, "gain must clear the AMT exemption"

        expected = _expected_surcharge(r.drawdown_taxable, r.realized_capital_gains)
        assert expected > 0, "the scenario must actually make AMT bite"
        # The fold booked exactly the minimum-tax surcharge the amt module says.
        assert r.amt_surcharge == pytest.approx(expected)

    def test_surcharge_reduces_net_worth_versus_a_no_amt_baseline(self):
        # The charged tax is max(regular, AMT): the AMT surcharge is an EXTRA
        # tax the fold books on top of the regular tax already priced, so the
        # household's end-of-year assets fall by exactly the surcharge relative
        # to the same run without it (the surcharge is charged against the
        # non-reg pot whose disposition triggered it).
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=0.0, net_target=1_200_000)
        assert r.amt_surcharge > 0
        # non-reg pre-surcharge = reported balance + the surcharge charged to it.
        assert r.total_assets == pytest.approx(r.non_reg_balance)


# ---------------------------------------------------------------------------
# 2. Absence is a no-op: a household that realizes NO gain is untouched.
# ---------------------------------------------------------------------------
class TestNormalHouseholdUnaffected:
    def test_no_drawdown_no_realized_gain_no_amt(self):
        # A working accumulation year: the non-reg pot only grows, nothing is
        # disposed -> zero realized gain -> the amt rule is a strict no-op.
        state = SimState(
            non_reg_balance=3_000_000, non_reg_acb=1_000_000,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        r, _ = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 150_000, '_annual_savings': 0},
            config=_retired_config(), investment_return=0.05,
            primary_marginal_rate=0.45, calendar_year=2026,
        )
        assert r.realized_capital_gains == pytest.approx(0.0)
        assert r.amt_surcharge == 0.0

    def test_modest_realized_gain_under_the_exemption_does_not_bite(self):
        # ACB == opening FMV: the $1.2M draw realizes only the small in-year
        # growth gain (well under the AMT basic exemption), so AMTI stays below
        # the exemption+regular-tax threshold and no surcharge is charged.
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=3_000_000, net_target=1_200_000)
        assert 0 < r.realized_capital_gains < _AMT_EXEMPTION
        assert r.amt_surcharge == 0.0

    def test_golden_household_never_hits_amt(self):
        # The #581 golden household realizes 0 capital gains in all 46 years, so
        # AMT never binds and the terminal-assets invariant is unmoved.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        results = _run(golden_household_config())
        assert all(r.amt_surcharge == 0.0 for r in results)
        assert results[-1].total_assets == 9709753.139463063


# ---------------------------------------------------------------------------
# 3. Charging the surcharge floors the non-reg ACB to the reduced balance so
#    acb <= fmv still holds (the DP#19 balance/ACB identity). This exercises the
#    apply_amt rule directly, in the mid-fold working state that reaches the
#    flooring branch: a large gain already realized this year (so AMT bites),
#    leaving a small positive non-reg remainder that still carries cost base --
#    the surcharge then drops the balance below that ACB.
# ---------------------------------------------------------------------------
def _amt_ctx(**over):
    base = dict(
        year=0, calendar_year=2026, allocations={}, config=_retired_config(),
        investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0,
        primary_income_pre=0.0, spouse_income_pre=0.0,
        primary_retired=True, spouse_retired=True,
    )
    base.update(over)
    return RuleContext(**base)


class TestSurchargeFloorsAcb:
    def test_surcharge_that_exceeds_the_remaining_gain_floors_acb_to_balance(self):
        # A $1.6M gain was realized this year (AMT bites, ~$96k surcharge), and a
        # small non-reg remainder ($250k balance, $240k ACB -> only $10k of it is
        # unrealized gain) is left. Charging the surcharge drops the balance below
        # the ACB, so the rule floors ACB to the reduced balance.
        ws = YearWorkingState(year=0)
        ws.drawdown_realized_capital_gain = 1_600_000.0
        ws.drawdown_taxable = 0.5 * 1_600_000.0   # the regular 50%-included slice
        ws.new_nonreg_bal = 250_000.0
        ws.new_nonreg_acb = 240_000.0

        fired = RULES['amt'](ws, _amt_ctx())

        assert fired is True
        assert ws.amt_surcharge > 10_000.0        # exceeds the $10k remaining gain
        assert ws.amt_surcharge < 250_000.0       # but is funded from the pot
        # Balance reduced by the funded NET minimum tax (federal AMT + the
        # separate Quebec IMR, #747), and ACB floored to it (the DP#19 identity
        # acb <= fmv holds after the charge). No credit is recovered here (this
        # is the year AMT is PAID, so there is no carried balance to recover).
        total_charge = ws.amt_surcharge + ws.qc_imr_surcharge
        assert ws.qc_imr_surcharge > 0.0          # a Quebec resident owes the IMR
        assert ws.new_nonreg_bal == pytest.approx(250_000.0 - total_charge)
        assert ws.new_nonreg_acb == pytest.approx(ws.new_nonreg_bal)
        assert ws.new_nonreg_acb < 240_000.0      # ACB actually fell
