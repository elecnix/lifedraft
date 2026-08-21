#!/usr/bin/env python3
"""Issue #1082: apply_amt must not silently discard minimum tax it cannot fund.

Before #1082, ``apply_amt`` funded its net minimum-tax charge via
``funded = min(net_charge, ws.new_nonreg_bal)`` and silently discarded the
unfunded remainder -- no field, no invariant, no raise. In the issue's
reported case (figures here are fabricated round numbers, DP#4/DP#15)
roughly $200k federal + $180k QC IMR -- $380k of assessed minimum tax --
landed on an EMPTY non-reg pot and simply vanished: understated tax,
overstated leveraged net worth (the same pro-leverage bias as
#1034/#1033/#1081). Newly reachable because #1043 wires
SM-servicing realized gains into the AMT base while the servicing rule drains
non-reg first.

The fix mirrors the codebase's existing unfundable-charge treatment (#681's
``heloc_interest_unfunded``, DP#32 -- report, never absorb): the assessed net
charge and its unfunded slice are surfaced on ``YearResult``
(``amt_net_charge`` / ``amt_unfunded``) and pinned by the registered
``amt_minimum_tax_accounted`` invariant. Also fixed here (filed in the same
issue): ``apply_amt`` now prices the AMT capital-gains gross-up with
``ctx.config.capital_gains_inclusion`` instead of a hardcoded ``0.5``.

Every test drives ``simulate_year_pure`` -- the engine's own fold -- never
hand-built engine state (DP#11/#18). Round numbers, no PII (DP#15).

Run: uv run pytest tests/test_issue_1082_amt_unfunded.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from simulation_state import SimState, simulate_year_pure, _default_canada_state
from simulation_config import SimulationConfig
from trajectory_invariants import run_invariant
from countries.canada.amt import AMTParameters, total_tax_with_amt
from countries.canada.tax_calc import (
    compute_non_refundable_credits, federal_tax_before_abatement,
    quebec_abatement_amount,
)
from tax_data import default_tax_provider


def _config(**over):
    base = dict(
        projection_years=1, investment_return=0.05, mortgage_balance=0,
        mortgage_rate=0.05, margin_available=0, province='quebec',
        family_members=[
            {'role': 'primary', 'gross_income': 0, 'birth_year': 1955},
            {'role': 'spouse', 'gross_income': 0, 'birth_year': 1957},
        ],
        children=[],
    )
    base.update(over)
    return SimulationConfig(**base)


def _run_year(non_reg_balance, non_reg_acb, net_target, **config_over):
    """One retirement year drawing ``net_target`` net from the non-reg pot,
    realizing capital gain -- the engine fold, not hand-built state."""
    state = SimState(
        non_reg_balance=non_reg_balance, non_reg_acb=non_reg_acb,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    result, _ = simulate_year_pure(
        state=state, year=0,
        allocations={'_primary_income': 0, '_annual_savings': 0},
        config=_config(**config_over), investment_return=0.05,
        primary_marginal_rate=0.53, retiree_marginal_rate=0.53,
        calendar_year=2026,
        drawdown_net_target=net_target, drawdown_order=['non_reg'],
        any_retired=True, retirement_spending_target=net_target,
    )
    return result


def _expected_federal_surcharge(taxable_income, realized_gain, inclusion,
                                province='quebec', year=2026):
    """The federal AMT surcharge straight from the amt module at a GIVEN
    inclusion rate -- the oracle the wired fold must reproduce."""
    provider = default_tax_provider()
    gross_fed = federal_tax_before_abatement(taxable_income, year, province, provider)
    abatement = quebec_abatement_amount(taxable_income, year, province, provider)
    nr = compute_non_refundable_credits(0, taxable_income, year, province, provider)['total']
    return total_tax_with_amt(
        regular_tax=max(0.0, gross_fed - abatement - nr),
        taxable_income=taxable_income,
        taxable_capital_gains=inclusion * realized_gain,
        capital_gains_inclusion=inclusion,
        nonrefundable_credits=nr,
        params=AMTParameters.for_year(year, provider),
    )['amt_surcharge']


# ---------------------------------------------------------------------------
# 1. The pin: minimum tax assessed against an EMPTY non-reg pot does not
#    silently evaporate (#1082's measured shape).
# ---------------------------------------------------------------------------
class TestUnfundedMinimumTaxIsSurfaced:
    def test_assessment_against_empty_pot_is_reported_not_discarded(self):
        # Drawing the whole $3M pot (ACB 0) realizes a ~$3.15M gain; the
        # year-end AMT assessment lands on an empty pot. Before #1082 the
        # entire charge vanished; now every dollar of it is reported.
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=0.0,
                      net_target=3_000_000)
        assert r.amt_surcharge > 100_000
        assert r.qc_imr_surcharge > 100_000
        assert r.non_reg_balance == pytest.approx(0.0)
        # Nothing was recovered (this is the year the tax is PAID), so the
        # net charge is the two surcharges -- and NONE of it was fundable.
        assert r.amt_credit_recovered == 0.0
        assert r.qc_imr_credit_recovered == 0.0
        assert r.amt_net_charge == pytest.approx(
            r.amt_surcharge + r.qc_imr_surcharge)
        assert r.amt_unfunded == pytest.approx(r.amt_net_charge)
        assert r.amt_unfunded > 300_000   # the reported case's order of magnitude

    def test_partially_fundable_charge_reports_only_the_unfunded_slice(self):
        # A target that drains the pot to just short of the charge: part of
        # the minimum tax is funded from the pot's last dollars, and ONLY the
        # remainder is reported unfunded.
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=0.0,
                      net_target=2_100_000)
        assert r.amt_net_charge > 0
        assert 0 < r.amt_unfunded < r.amt_net_charge
        # The pot funded everything it could before running dry.
        assert r.non_reg_balance == pytest.approx(0.0)

    def test_fully_funded_charge_reports_zero_unfunded(self):
        # A charge well inside the remaining pot: nothing unfunded, and the
        # pot shrinks by exactly the net charge (no regression on the #710
        # path).
        r = _run_year(non_reg_balance=3_000_000, non_reg_acb=0.0,
                      net_target=2_000_000)
        assert r.amt_net_charge > 0
        assert r.amt_unfunded == pytest.approx(0.0)
        assert r.non_reg_balance > 0

    def test_no_minimum_tax_no_unfunded(self):
        # A normal accumulation year with no realized gain: the amt rule is a
        # strict no-op and reports neither a charge nor a shortfall.
        state = SimState(
            non_reg_balance=3_000_000, non_reg_acb=1_000_000,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        r, _ = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 150_000, '_annual_savings': 0},
            config=_config(), investment_return=0.05,
            primary_marginal_rate=0.45, calendar_year=2026,
        )
        assert r.realized_capital_gains == pytest.approx(0.0)
        assert r.amt_net_charge == pytest.approx(0.0)
        assert r.amt_unfunded == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. The adjacent inconsistency: the AMT gross-up uses the DECLARED inclusion
#    rate, not a hardcoded 0.5.
# ---------------------------------------------------------------------------
class TestInclusionRateIsDeclaredNotHardcoded:
    def test_surcharge_tracks_the_declared_inclusion_rate(self):
        # Same scenario at the default 0.5 and at a declared 0.75: each run
        # must match the amt-module oracle priced at ITS OWN declared rate --
        # impossible if the fold hardcodes 0.5.
        r_half = _run_year(3_000_000, 0.0, 2_500_000)
        r_three_quarters = _run_year(3_000_000, 0.0, 2_500_000,
                                     capital_gains_inclusion=0.75)
        assert r_half.amt_surcharge == pytest.approx(_expected_federal_surcharge(
            r_half.drawdown_taxable, r_half.realized_capital_gains, 0.5))
        assert r_three_quarters.amt_surcharge == pytest.approx(
            _expected_federal_surcharge(
                r_three_quarters.drawdown_taxable,
                r_three_quarters.realized_capital_gains, 0.75))

    def test_declared_inclusion_actually_moves_the_assessment(self):
        # The declaration is not inert: raising the inclusion rate raises the
        # AMT base (and the regular tax measured against it) and changes the
        # assessment. (A hardcoded 0.5 would produce identical results.)
        r_half = _run_year(3_000_000, 0.0, 2_500_000)
        r_full = _run_year(3_000_000, 0.0, 2_500_000,
                           capital_gains_inclusion=1.0)
        assert r_half.amt_surcharge != pytest.approx(r_full.amt_surcharge)


# ---------------------------------------------------------------------------
# 3. The registered invariant pins the bookkeeping from the trajectory.
# ---------------------------------------------------------------------------
class TestAmtMinimumTaxAccountedInvariant:
    def test_holds_on_every_scenario_above(self):
        for r in (
            _run_year(3_000_000, 0.0, 3_000_000),   # fully unfunded
            _run_year(3_000_000, 0.0, 2_100_000),   # partially funded
            _run_year(3_000_000, 0.0, 2_000_000),   # fully funded
        ):
            assert run_invariant('amt_minimum_tax_accounted', [r]) == []

    def test_flags_a_shortfall_larger_than_the_charge(self):
        class R:
            amt_net_charge = 100_000.0
            amt_unfunded = 150_000.0
            amt_surcharge = 100_000.0
            qc_imr_surcharge = 0.0
        violations = run_invariant('amt_minimum_tax_accounted', [R()])
        assert len(violations) == 1
        assert 'does not fit inside' in str(violations[0])

    def test_flags_an_unfunded_slice_without_an_assessment(self):
        class R:
            amt_net_charge = 0.0
            amt_unfunded = 50_000.0
            amt_surcharge = 0.0
            qc_imr_surcharge = 0.0
        violations = run_invariant('amt_minimum_tax_accounted', [R()])
        assert len(violations) == 1
        assert 'no assessed net minimum-tax charge' in str(violations[0])

    def test_flags_an_unfunded_slice_in_a_refund_year(self):
        # Recovered credits exceed the new surcharges: a net REFUND funds
        # nothing, so a reported shortfall that year is bookkeeping garbage.
        class R:
            amt_net_charge = 0.0          # floored: refund years report no charge
            amt_unfunded = 50_000.0
            amt_surcharge = 10_000.0
            qc_imr_surcharge = 5_000.0
        violations = run_invariant('amt_minimum_tax_accounted', [R()])
        assert len(violations) == 1
        assert 'no assessed net minimum-tax charge' in str(violations[0])

    def test_flags_a_shortfall_with_no_assessment_despite_a_net_charge(self):
        # The engine cannot produce this shape (a positive net charge implies
        # a positive surcharge), which is exactly why it is bookkeeping
        # garbage worth its own guard: a net charge survived the credit
        # offset while the surcharge fields read zero, yet a shortfall was
        # reported against it.
        class R:
            amt_net_charge = 80_000.0
            amt_unfunded = 20_000.0
            amt_surcharge = 0.0
            qc_imr_surcharge = 0.0
        violations = run_invariant('amt_minimum_tax_accounted', [R()])
        assert len(violations) == 1
        assert 'no minimum-tax surcharge was assessed' in str(violations[0])


# ---------------------------------------------------------------------------
# 4. The invariant over a FULL RUN: FamilySimulation.run() end to end.
# ---------------------------------------------------------------------------
class TestInvariantOverFullRun:
    def test_whole_trajectory_is_accounted(self):
        # A multi-year full run whose working years realize no capital gains:
        # apply_amt is a strict no-op every year, so the invariant evaluates
        # (and holds) on all 46 reported years of a real fold -- not just the
        # single years simulate_year_pure produces above.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        results = _run(golden_household_config())
        assert all(r.amt_net_charge == 0.0 and r.amt_unfunded == 0.0
                   for r in results)
        assert run_invariant('amt_minimum_tax_accounted', results) == []
