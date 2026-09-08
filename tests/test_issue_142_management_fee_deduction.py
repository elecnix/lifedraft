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


def test_mixed_pot_zero_opening_records_zero_rate():
    """The #136 mixed-pot split, management-fee edition (adapter lines 341-342):
    a MIXED non_reg pot (one flagged account, one non-flagged) where every
    account opens at $0 falls back to rate 0.0 -- charging the non-flagged
    money would invent a fee the household never agreed to. Mirror of
    #136's ``mer_mixed_pot_zero_fee_unmodeled`` fixture shape (DP#13/DP#15)."""
    doc = {
        "people": [],
        "accounts": [
            {"id": "acct_flagged", "kind": "non_reg",
             "balance": {"amount": 0.0, "as_of": "2026-01-01"},
             "acb": 0.0, "holdings": [], "beneficiary": None,
             "successor_holder": None, "owner": {"person": "primary"},
             "management_fee": 0.005},
            {"id": "acct_plain", "kind": "non_reg",
             "balance": {"amount": 0.0, "as_of": "2026-01-01"},
             "acb": 0.0, "holdings": [], "beneficiary": None,
             "successor_holder": None, "owner": {"person": "spouse"}},
        ],
    }
    out = _map_account_overrides(doc)
    assert out["management_fee_rate"] == {
        "non_reg": {"management_fee_rate": 0.0}}


def test_single_flagged_zero_opening_falls_back_to_max():
    """The #136 single-flagged split, management-fee edition (adapter lines
    343-344): a non_reg pot where EVERY account declares the fee and opens at
    $0 -- the pot IS the fee'd money -- falls back to the max declared rate so
    a future funded state is not silently fee-free."""
    doc = _doc_non_reg_with_fee(fee=0.005, balance=0.0)
    out = _map_account_overrides(doc)
    assert out["management_fee_rate"] == {
        "non_reg": {"management_fee_rate": 0.005}}


def test_no_declared_fee_records_nothing():
    """No declared fee -> empty map (DP#32: absence is absence, golden no-op)."""
    doc = _doc_non_reg_with_fee()
    del doc["accounts"][0]["management_fee"]
    out = _map_account_overrides(doc)
    assert out["management_fee_rate"] == {}


def test_mer_only_account_gets_no_management_fee():
    """An account declaring only `mer` populates mer_drag and records NO
    management fee -- the two fees are distinct inputs (DP#8), and a MER
    declaration must never silently grow into a s.20(1)(e) fee."""
    doc = _doc_non_reg_with_fee()
    del doc["accounts"][0]["management_fee"]
    doc["accounts"][0]["mer"] = 0.01
    out = _map_account_overrides(doc)
    assert out["management_fee_rate"] == {}
    assert out["mer_drag"] == {"non_reg": {"mer_rate": 0.01}}


def test_opening_pot_balance_lif_and_fhsa_pots():
    """``_opening_pot_balance`` maps the ``lif`` and ``fhsa`` pot kinds to their
    opening fields (rule lines 71-74) -- every adapter-emittable kind has a
    real opening balance, never a silently unfunded pot (DP#32)."""
    from rule_registry import YearWorkingState
    from rules_management_fee import _opening_pot_balance

    ws = YearWorkingState()
    ws.opening_lif_balance = 40000.0
    ws.opening_fhsa_balance = 8000.0
    assert _opening_pot_balance(ws, 'lif') == 40000.0
    assert _opening_pot_balance(ws, 'fhsa') == 8000.0


def _state_with_tfsa_and_lira(primary_tfsa=120000.0, spouse_tfsa=30000.0,
                              lira=54000.0):
    """A real SimState carrying per-adult TFSA (primary + spouse) and a LIRA,
    the stores the engine's own ``YearWorkingState.from_state`` stamps the
    ``opening_*`` scalars from (issue #700/#643)."""
    from simulation_state import SimState, _default_canada_state
    canada = _default_canada_state()
    canada['adult_tfsa'] = {
        'primary': {'balance': primary_tfsa, 'room': 0.0},
        'spouse': {'balance': spouse_tfsa, 'room': 0.0},
    }
    canada['adult_lira'] = {'primary': {
        'balance': lira, 'birth_year': 1979, 'jurisdiction': 'quebec',
        'reference_rate': 0.06, 'conversion_year': 0,
    }}
    return SimState(
        non_reg_balance=0.0, non_reg_acb=0.0, mortgage_balance=0.0,
        heloc_balance=0.0, jurisdiction_state={'canada': canada})


def test_tfsa_pot_fee_charged_on_summed_opening_balances():
    """A declared ``tfsa`` management fee is charged on the SUM of the
    primary's and the spouse's opening TFSA balances (rule line 67) -- the
    registered rule over an engine-stamped working state (no hand-built
    openings), and no slice is deductible: the shelter means the fee is
    real cash with NO s.20(1)(e) charge against other income."""
    from rule_registry import YearWorkingState
    from rules_management_fee import apply_management_fee
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "tfsa": {"management_fee_rate": 0.001}}
    ws = YearWorkingState.from_state(_state_with_tfsa_and_lira(), {}, 0)
    assert (ws.opening_tfsa_primary_balance,
            ws.opening_tfsa_spouse_balance) == (120000.0, 30000.0)
    assert apply_management_fee(ws, _ctx(config))
    assert ws.management_fee == 150.0  # 0.001 x (120000 + 30000)
    assert ws.management_fee_deductible == 0.0


def test_lira_pot_fee_charged_on_opening_balance():
    """A declared ``lira`` management fee is charged on the LIRA pot's opening
    balance (rule line 70), read from the per-adult store the engine stamps
    -- again real cash, no deduction (a LIRA is registered money)."""
    from rule_registry import YearWorkingState
    from rules_management_fee import apply_management_fee
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "lira": {"management_fee_rate": 0.002}}
    ws = YearWorkingState.from_state(_state_with_tfsa_and_lira(), {}, 0)
    assert ws.opening_lira_balance == 54000.0
    assert apply_management_fee(ws, _ctx(config))
    assert ws.management_fee == 108.0  # 0.002 x 54000
    assert ws.management_fee_deductible == 0.0


def test_opening_pot_balance_unknown_kind_is_a_loud_failure():
    """An adapter-emitted kind with no opening field raises ValueError
    (rule line 75) -- a loud failure, never a silently unfunded pot (DP#32)."""
    import pytest
    from rule_registry import YearWorkingState
    from rules_management_fee import _opening_pot_balance

    with pytest.raises(ValueError, match="unknown account kind"):
        _opening_pot_balance(YearWorkingState(), 'mystery_kind')


def test_declared_zero_fee_rate_is_a_value_not_absence():
    """DP#32: an explicitly declared 0.0 fee rate is a DECLARED fee-free fact
    (rule line 101's continue), not an absent declaration -- the household
    said zero. Behaviourally both book no fee; the distinction the rule
    protects is that the zero declaration is consumed as a fact, not skipped
    as missing input, so a kind explicitly set to 0.0 coexisting with a
    charged kind still books only the charged fee."""
    from rule_registry import YearWorkingState
    from rules_management_fee import apply_management_fee
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "non_reg": {"management_fee_rate": 0.0},
        "rrsp": {"management_fee_rate": 0.005}}
    ws = YearWorkingState()
    ws.opening_non_reg_balance = 100000.0
    ws.opening_rrsp_balance = 200000.0
    assert apply_management_fee(ws, _ctx(config))
    assert ws.management_fee == 1000.0  # rrsp only; the declared 0.0 adds nothing
    assert ws.management_fee_deductible == 0.0


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


def test_no_declared_fee_is_a_fold_noop():
    """DP#32 golden no-op: no declared `management_fee` anywhere -> the rule
    refuses to fire and leaves BOTH working-state outputs at their exact
    0.0 defaults, so the solvency identity's `+ ws.management_fee` and the
    sm_interest pooling add a byte-identical zero. Absence is absence, never
    a zeroed fee silently applied."""
    from rule_registry import YearWorkingState
    from rules_management_fee import apply_management_fee
    from simulation_config import SimulationConfig

    config = SimulationConfig()  # account_management_fee_rate empty
    ws = YearWorkingState()
    ws.opening_non_reg_balance = 100000.0
    ws.opening_rrsp_balance = 200000.0
    assert not apply_management_fee(ws, _ctx(config))
    assert ws.management_fee == 0.0
    assert ws.management_fee_deductible == 0.0


def test_management_fee_rate_does_not_touch_growth_drag():
    """A declared management_fee must NOT change the growth net rate -- the
    MER path (#136) is the only growth drag. A non_reg pot with ONLY a
    management fee grows at the full gross rate; adding a `mer` is what
    reduces it."""
    from rule_registry import RuleContext, YearWorkingState
    from rules_growth import _blended_pot_rate
    from simulation_config import SimulationConfig

    config = SimulationConfig()
    config.account_management_fee_rate = {
        "non_reg": {"management_fee_rate": 0.005}}
    ctx = _ctx(config)
    assert _blended_pot_rate(ctx, 'non_reg', 100000.0) == ctx.investment_return

    config.account_mer_drag = {"non_reg": {"mer_rate": 0.01}}
    ctx_mer = _ctx(config)
    assert (_blended_pot_rate(ctx_mer, 'non_reg', 100000.0)
            == ctx.investment_return - 0.01)


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
