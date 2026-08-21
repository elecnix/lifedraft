#!/usr/bin/env python3
"""Issue #747: the three AMT refinements #710 deferred, wired into the run.

#710 wired the basic ``max(regular, AMT)`` surcharge. #747 adds:
  1. the 7-year AMT carry-forward (ITA s.120.2): AMT paid becomes a credit
     recovered against regular tax in a later year where regular tax exceeds the
     minimum amount;
  2. the 50%-of-non-refundable-credits reduction to the minimum (s.127.531);
  3. Quebec's separate impôt minimum de remplacement (TP-776.42).

These drive the wired ``amt`` rule directly (its real production body) and thread
the minimum-tax credit balance through the pure fold's state, proving the
cross-year recovery end-to-end. Lean, invariant-based, round numbers, no PII.

Run: uv run pytest tests/test_issue_747_amt_parts.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_state import SimState, simulate_year_pure, _default_canada_state
from simulation_config import SimulationConfig
from simulation_rules import RULES, RuleContext, YearWorkingState
from countries.canada.amt import AMTCredit


def _quebec_config():
    return SimulationConfig(
        projection_years=1, investment_return=0.05, mortgage_balance=0,
        mortgage_rate=0.05, margin_available=0, province='quebec',
        family_members=[
            {'role': 'primary', 'gross_income': 0, 'birth_year': 1955},
            {'role': 'spouse', 'gross_income': 0, 'birth_year': 1957},
        ],
        children=[],
    )


def _amt_ctx(**over):
    """A RuleContext exercising the wired ``amt`` rule in isolation (mirrors the
    #710 rule-mechanics harness), with the opening credit balances #747 adds."""
    base = dict(
        year=0, calendar_year=2026, allocations={}, config=_quebec_config(),
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


# ---------------------------------------------------------------------------
# 1. Carry-forward: pay AMT once, recover it in a later year (ITA s.120.2).
# ---------------------------------------------------------------------------
class TestCarryForwardRecoversAmtPaid:
    def test_paying_year_books_a_credit_and_recovers_nothing(self):
        ws = YearWorkingState(year=0)
        ws.drawdown_realized_capital_gain = 1_600_000.0
        ws.drawdown_taxable = 0.5 * 1_600_000.0
        ws.new_nonreg_bal = 3_000_000.0
        ws.new_nonreg_acb = 3_000_000.0

        fired = RULES['amt'](ws, _amt_ctx(calendar_year=2026))

        assert fired is True
        assert ws.amt_surcharge > 0                       # AMT bit
        assert ws.amt_credit_recovered == 0.0             # nothing to recover yet
        # the surcharge is booked as a fresh 2026 credit to carry forward
        closing = ws.amt_credit_closing
        assert [(c.year, round(c.amount, 2)) for c in closing] == \
            [(2026, round(ws.amt_surcharge, 2))]

    def test_later_year_with_room_recovers_the_credit_and_refunds_the_pot(self):
        # A later year: no gain, but a $400k salary makes regular federal tax
        # exceed the (no-gain) minimum amount -> recoverable room. The carried
        # $30k credit is recovered up to that room and refunded to the pot.
        opening = (AMTCredit(2026, 30_000.0),)
        ws = YearWorkingState(year=3)
        ws.new_nonreg_bal = 100_000.0
        ws.new_nonreg_acb = 100_000.0

        fired = RULES['amt'](ws, _amt_ctx(
            calendar_year=2029, amt_credit_opening=opening,
            primary_retired=False, primary_income_pre=400_000.0))

        assert fired is True
        assert ws.amt_surcharge == 0.0                    # no new AMT (no gain)
        assert ws.amt_credit_recovered > 0.0              # credit recovered
        # the recovered credit is a refund reinvested in the non-reg pot
        assert ws.new_nonreg_bal == pytest.approx(100_000.0 + ws.amt_credit_recovered)
        assert ws.new_nonreg_acb == pytest.approx(100_000.0 + ws.amt_credit_recovered)
        # closing balance = opening minus what was recovered
        remaining = sum(c.amount for c in ws.amt_credit_closing)
        assert remaining == pytest.approx(30_000.0 - ws.amt_credit_recovered)

    def test_recovery_is_capped_by_the_carried_balance(self):
        # Huge room but only a $5k credit -> at most $5k comes back.
        opening = (AMTCredit(2026, 5_000.0),)
        ws = YearWorkingState(year=3)
        ws.new_nonreg_bal = 100_000.0
        ws.new_nonreg_acb = 100_000.0
        RULES['amt'](ws, _amt_ctx(
            calendar_year=2029, amt_credit_opening=opening,
            primary_retired=False, primary_income_pre=1_000_000.0))
        assert ws.amt_credit_recovered == pytest.approx(5_000.0)
        assert ws.amt_credit_closing == ()


# ---------------------------------------------------------------------------
# 2. The credit balance threads through the pure fold's state (DP#26).
# ---------------------------------------------------------------------------
class TestCreditThreadsThroughFoldState:
    def test_a_gain_year_writes_the_credit_into_next_state(self):
        state = SimState(
            non_reg_balance=3_000_000, non_reg_acb=0.0,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        _, next_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 0, '_annual_savings': 0},
            config=_quebec_config(), investment_return=0.05,
            primary_marginal_rate=0.53, retiree_marginal_rate=0.53,
            calendar_year=2026,
            drawdown_net_target=1_200_000, drawdown_order=['non_reg'],
            any_retired=True, retirement_spending_target=1_200_000,
        )
        buckets = next_state.jurisdiction_state['canada']['amt_credit_buckets']
        assert buckets, "the AMT paid this year must be carried forward as a credit"
        assert buckets[0].year == 2026
        assert buckets[0].amount > 0

    def test_a_no_gain_no_credit_year_carries_an_empty_balance(self):
        state = SimState(
            non_reg_balance=3_000_000, non_reg_acb=1_000_000,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        _, next_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 150_000, '_annual_savings': 0},
            config=_quebec_config(), investment_return=0.05,
            primary_marginal_rate=0.45, calendar_year=2026,
        )
        assert next_state.jurisdiction_state['canada']['amt_credit_buckets'] == []


# ---------------------------------------------------------------------------
# 3. Quebec IMR: a Quebec resident owes the provincial minimum, booked apart.
# ---------------------------------------------------------------------------
class TestQuebecIMRIsAssessed:
    def test_quebec_resident_owes_a_separate_imr_surcharge(self):
        ws = YearWorkingState(year=0)
        ws.drawdown_realized_capital_gain = 1_600_000.0
        ws.drawdown_taxable = 0.5 * 1_600_000.0
        ws.new_nonreg_bal = 3_000_000.0
        ws.new_nonreg_acb = 3_000_000.0

        RULES['amt'](ws, _amt_ctx(calendar_year=2026))

        assert ws.qc_imr_surcharge > 0.0                  # the provincial minimum
        # it is a DISTINCT tax, not folded into the federal amt_surcharge
        assert ws.qc_imr_surcharge != ws.amt_surcharge

    def test_non_quebec_resident_owes_no_imr(self):
        cfg = _quebec_config()
        cfg.province = 'ontario'
        ws = YearWorkingState(year=0)
        ws.drawdown_realized_capital_gain = 1_600_000.0
        ws.drawdown_taxable = 0.5 * 1_600_000.0
        ws.new_nonreg_bal = 3_000_000.0
        ws.new_nonreg_acb = 3_000_000.0

        RULES['amt'](ws, _amt_ctx(calendar_year=2026, config=cfg))

        assert ws.qc_imr_surcharge == 0.0                 # IMR is Quebec-only
