"""Issue #142: the s.20(1)(e) management-fee deduction.

A declared account-level ``management_fee`` RATE (distinct from ``mer``,
DP#8) is a real annual cash fee. On a NON-REGISTERED account the fee is a
deductible carrying charge (ITA s.20(1)(e)) and pools into the same
s.20(1)(c) machinery the SM interest uses (bracket-fill valuation, the TA
s.336.0.1 QC cap, retirement gating). On REGISTERED kinds (RRSP/TFSA) it is
a real cash fee with NO deduction -- the shelter means the expense is not a
deductible charge against other income.

Fixtures are synthetic round numbers (DP#15/#4).
"""
from __future__ import annotations

from contract_accounts import _map_account_overrides


def _doc_non_reg_with_fee(fee=0.005, balance=100000.0):
    return {
        "people": [],
        "accounts": [
            {"id": "acct_nonreg", "kind": "non_reg",
             "balance": {"amount": balance, "as_of": "2026-01-01"},
             "acb": balance, "holdings": [], "beneficiary": None,
             "successor_holder": None, "owner": {"person": "primary"},
             "management_fee": fee},
        ],
    }


def test_adapter_collects_management_fee_rate():
    """A declared management_fee lands in a pot-keyed management_fee_rate."""
    out = _map_account_overrides(_doc_non_reg_with_fee())
    assert out["management_fee_rate"] == {"non_reg": {"management_fee_rate": 0.005}}


def test_no_declared_fee_records_nothing():
    """No declared fee -> empty map (DP#32: absence is absence, golden no-op)."""
    doc = _doc_non_reg_with_fee()
    del doc["accounts"][0]["management_fee"]
    out = _map_account_overrides(doc)
    assert out["management_fee_rate"] == {}


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


def test_nonreg_management_fee_is_deductible():
    """The non-registered slice of the fee is a deductible carrying charge:
    it pools into the s.20(1)(c) deduction the sm_interest rule values at
    bracket-fill (ws.sm_interest_deduction). $100,000 pot x 0.5% = $500."""
    from rule_registry import YearWorkingState
    from rules_leverage import apply_sm_interest
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "non_reg": {"management_fee_rate": 0.005}}
    ws = YearWorkingState()
    ws.new_nonreg_bal = 100000.0
    ws.management_fee = 500.0
    # A pre-existing SM-interest pot keeps the rule off its no-traced-deduction
    # early return; the fee's non-reg slice must POOL into that deduction
    # ($500 traced + $500 fee = $1000 on ws.sm_interest_deduction).
    ws.advance_deductible_interest = 500.0
    ws.advance_deductible_balance = 0.0
    ws.new_tracing = {'total_advances': 1000.0, 'investment_advances': 1000.0,
                      'rrsp_advances': 0.0, 'tfsa_advances': 0.0,
                      'personal_draws': 0.0}
    ctx = _ctx(config)
    fired = apply_sm_interest(ws, ctx)
    assert fired
    assert ws.sm_interest_deduction == 1000.0


def test_registered_management_fee_cash_no_deduction():
    """RRSP/TFSA fees are real cash but add NOTHING to the deduction."""
    from rule_registry import YearWorkingState
    from rules_leverage import apply_sm_interest
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "rrsp": {"management_fee_rate": 0.005},
        "tfsa": {"management_fee_rate": 0.005}}
    ws = YearWorkingState()
    ws.opening_rrsp_balance = 100000.0
    ws.opening_tfsa_primary_balance = 50000.0
    ws.management_fee = 750.0          # (100000 + 50000) * 0.005, real cash
    ws.management_fee_deductible = 0.0  # registered: never deductible
    ctx = _ctx(config)
    apply_sm_interest(ws, ctx)
    assert ws.sm_interest_deduction == 0.0
    assert ws.management_fee == 750.0
