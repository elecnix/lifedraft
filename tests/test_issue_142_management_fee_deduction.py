"""Issue #142: the s.20(1)(e) management-fee deduction.

A declared account-level ``management_fee`` RATE (distinct from ``mer``,
DP#8) is a real annual cash fee. On a NON-REGISTERED account the fee is a
deductible carrying charge (ITA s.20(1)(e)) and pools into the same
s.20(1)(c) machinery the SM interest uses (bracket-fill valuation, the TA
s.336.0.1 QC cap, retirement gating). On REGISTERED kinds (RRSP/TFSA) it is
a real cash fee with NO deduction -- the shelter means the expense is not a
deductible charge against other income.

Fixtures are synthetic round numbers (DP#15/#4). Every engine-behaviour test
drives the registered fold rules (`apply_management_fee`,
`apply_sm_interest`, `apply_amt`) -- never a hand-built copy of their math
(DP#11).
"""
from __future__ import annotations

def _ctx(config, retired=False, taxable_income=0.0):
    """A minimal live-fold-shaped RuleContext: year_brackets supplied, so the
    deduction is valued at bracket-fill (the #1033 path), not flat-rate."""
    from rule_registry import RuleContext
    empty_brackets = [{"upper": None, "rate": 0.0}]  # flat 0%: value == 0
    return RuleContext(
        year=0, calendar_year=2026, allocations={}, config=config,
        investment_return=0.06, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.4, spouse_marginal_rate=0.3,
        resp_data=None, fhsa_contribution=0.0,
        rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0,
        drawdown_order=None, rrif_min_rate_primary=0.0,
        rrif_min_rate_spouse=0.0, drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0, primary_retired=retired,
        year_brackets=empty_brackets,
        primary_taxable_income=taxable_income,
    )


def test_amt_noop_gate_includes_carrying_charges():
    """Issue #142's AMT fix (a pre-existing bug): the no-op gate must NOT
    fire in a no-gain year that booked a large s.20(1)(c) deduction -- the
    s.127.52(1)(j)(ii) half-add-back can lift AMTI above regular taxable
    income. Passing the gate is visible via ws.amt_taxable_income, which the
    old gate returned before ever writing."""
    from rule_registry import YearWorkingState
    from rules_amt import apply_amt
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    ws = YearWorkingState()
    ws.drawdown_taxable = 100000.0
    ws.sm_interest_deduction = 10000.0
    assert not apply_amt(ws, _ctx(config))  # no surcharge, but assessed
    assert ws.amt_taxable_income == 100000.0


def test_amt_half_add_back_raises_the_surcharge():
    """The s.127.52(1)(j)(ii) half-add-back is live: identical gain years
    differing only in the booked s.20(1)(c) deduction produce a STRICTLY
    higher AMT surcharge for the carrying-charge household. Before the fix
    both runs priced the same (the add-back was dormant)."""
    from rule_registry import YearWorkingState
    from rules_amt import apply_amt
    from simulation_config import SimulationConfig

    def _run(deduction):
        config = SimulationConfig()
        ws = YearWorkingState()
        ws.drawdown_realized_capital_gain = 500000.0
        ws.drawdown_taxable = 250000.0  # the 50%-included regular slice
        ws.sm_interest_deduction = deduction
        apply_amt(ws, _ctx(config))
        return ws.amt_surcharge

    assert _run(10000.0) > _run(0.0)
