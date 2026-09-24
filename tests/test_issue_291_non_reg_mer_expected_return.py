#!/usr/bin/env python3
"""Issue #291: a declared ``mer`` / ``expected_return`` on a ``non_reg`` account
must change how the non-reg pot grows on the DP#27 after-tax path.

Before #291, ``apply_non_reg_growth`` read the non_reg account's declared fee
and per-account return override only when the after-tax rate happened to EQUAL
the gross rate (a float-equality gate against ``ctx.investment_return``). The fold
never supplies ``None`` for the after-tax rate (``_non_reg_after_tax_return_for``
always returns a number), so for any household with a positive marginal rate and
a taxable distribution yield the gate was false and both declared inputs were
silently dropped: a 2% MER fund and a 0% one projected to the same balance.

The fix applies both as an exact linear shift of the after-tax rate:

    non_reg rate = atr(gross) + (override-blended gross - gross) - mer_rate

which equals evaluating ``_non_reg_after_tax_return_for`` at the fee-net,
override-blended gross, because its after-tax-yield term does not depend on
the gross return. The Smith-Manoeuvre sleeve keeps the UNSHIFTED shared taxable
rate: it is not a declared account, so it never inherits another account's fee.

What these tests pin:

- (a)/(b) fold level: contract -> ``input_contract.to_internal_config`` ->
  ``SimulationConfig.from_dict`` -> ``FamilySimulation.run()``, yearly AND
  monthly, asserting the engine's ``YearResult.non_reg_balance`` (DP#11/DP#26:
  no hand-built working state, no call to the rate helper).
- (c) the golden household (which declares neither input) is byte-identical.
- (d) the registered rules, driven with a NON-None after-tax rate (the shape the
  fold always produces), so no unit test can pass on the ``None`` fallback alone.

All data is the shipped synthetic example contract or fabricated round numbers
(DP#4/DP#15).
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import contract_schema
import input_contract as ic
import simulation
import simulation_rules  # noqa: F401 -- populates RULES via rules_* imports
import trajectory_invariants
from countries.canada.adapter import CanadaAdapter
from rule_registry import RULES, RuleContext, YearWorkingState
from simulation import FamilySimulation
from simulation_config import SimulationConfig

_KEEP = {"p1", "p2", "ca", "cb"}
_YEARS = 5
# The example contract's one household non_reg account that survives the trim
# (joint p1/p2): its declared opening balance is the override_balance / the
# opening pot the fee is charged on in year 1.
_OPENING_NONREG = 85_000


def _owners(acc):
    o = acc["owner"]
    if isinstance(o, str):
        return {o}
    if "joint" in o:
        return {j["person"] for j in o["joint"]}
    return {o["person"]}


def _example_doc():
    """The shipped synthetic example contract, trimmed to the p1/p2 household.

    Unlike tests/test_issue_691_mer.py's helper, this KEEPS the joint p1/p2
    non_reg account (the one #291 is about) -- an account survives when every
    one of its owners survives the trim.
    """
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = copy.deepcopy(json.load(f))
    doc["people"] = [p for p in doc["people"] if p["id"] in _KEEP]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in _KEEP]
    doc["accounts"] = [a for a in doc["accounts"] if _owners(a) <= _KEEP]
    return doc


def _non_reg_accounts(doc):
    return [a for a in doc["accounts"] if a["kind"] == "non_reg"]


def _run_contract(*, time_step, strip_holdings=False, **non_reg_fields):
    """Declare ``non_reg_fields`` on every non_reg account, map the contract
    through the real adapter, and drive the fold. Returns (cfg, sim, results)."""
    doc = _example_doc()
    nr = _non_reg_accounts(doc)
    assert [a["balance"]["amount"] for a in nr] == [_OPENING_NONREG], (
        "fixture drift: expected exactly the joint $85,000 non_reg account")
    for acc in nr:
        if strip_holdings:
            acc["holdings"] = []
        for k, v in non_reg_fields.items():
            if v is _ABSENT:
                acc.pop(k, None)
            else:
                acc[k] = v
    contract_schema.validate_contract(doc)
    cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
    cfg.projection_years = _YEARS
    cfg.time_step = time_step
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    return cfg, sim, sim.run()


_ABSENT = object()


def _assert_on_after_tax_path(cfg, sim, results):
    """Precondition: the fold's after-tax rate for this contract is BELOW the
    gross rate -- the exact path the old equality gate skipped."""
    gross = cfg.investment_return
    primary_rate = results[0].primary_marginal
    assert primary_rate > 0
    atr = simulation._non_reg_after_tax_return_for(
        0, primary_rate, gross, portfolio=sim._portfolio,
        non_reg_yield_rate=cfg.non_reg_yield_rate, province=cfg.province)
    assert atr < gross, (atr, gross)


def _assert_registered_and_acb_untouched(base, variant):
    """I6: the fee / override touch only the non_reg FMV in year 1; registered
    pots and the non_reg cost basis (DP#19) are byte-identical."""
    for field in ("primary_rrsp", "spousal_rrsp", "spouse_rrsp", "total_rrsp",
                  "primary_tfsa", "spouse_tfsa", "total_tfsa",
                  "lira_balance", "lif_balance", "non_reg_acb"):
        assert getattr(base[0], field) == getattr(variant[0], field), field


def _assert_money_invariants(results):
    for name in ("no_nan_or_inf", "no_negative_balances", "acb_le_fmv"):
        trajectory_invariants.assert_invariant(name, results)


_TIME_STEPS = ["yearly", "monthly"]


# ── (a) fold level: a declared non_reg MER lowers the non_reg balance ─────────

@pytest.mark.parametrize("time_step", _TIME_STEPS)
def test_declared_non_reg_mer_lowers_year1_balance_through_fold(time_step):
    cfg_b, sim_b, base = _run_contract(time_step=time_step, mer=0.0)
    cfg_v, sim_v, variant = _run_contract(time_step=time_step, mer=0.02)

    # Preconditions: the declaration reached the engine's config (present and
    # zero in the baseline, 2% in the variant), and the fold runs the DP#27
    # after-tax path with atr < gross.
    assert cfg_b.account_mer_drag["non_reg"]["mer_rate"] == 0.0
    assert cfg_v.account_mer_drag["non_reg"]["mer_rate"] == 0.02
    _assert_on_after_tax_path(cfg_v, sim_v, variant)

    diff = base[0].non_reg_balance - variant[0].non_reg_balance
    # The fee is 2% of the pre-growth pot P, and opening <= P <= base year-1
    # balance for an accumulating household. Before #291 this was exactly 0.0.
    assert 0.02 * _OPENING_NONREG <= diff <= 0.02 * base[0].non_reg_balance, diff

    # I4: the fee is charged EVERY year, not once. A merely growing gap would
    # not prove that (a one-off fee's gap also compounds), so each year's gap
    # must exceed the prior gap compounded at the full GROSS rate (an upper
    # bound on the variant's own growth rate) by at least 2% of the opening
    # pot: gap_t - gap_{t-1}*(1+gross) ~= 0.02 * (variant pot) for a recurring
    # fee, and <= 0 for a fee charged only once.
    gross = cfg_v.investment_return
    gaps = [b.non_reg_balance - v.non_reg_balance for b, v in zip(base, variant)]
    for t in range(1, len(gaps)):
        assert gaps[t] - gaps[t - 1] * (1 + gross) >= 0.02 * _OPENING_NONREG, (t, gaps)

    _assert_registered_and_acb_untouched(base, variant)
    _assert_money_invariants(variant)


@pytest.mark.parametrize("time_step", _TIME_STEPS)
def test_declared_non_reg_mer_applied_once_when_atr_equals_gross(time_step):
    """The coincident case at fold level: a non_reg account with no holdings
    has zero declared yield, so atr == gross. The old gate happened to fire
    here; the fee must still be subtracted exactly ONCE (no double count)."""
    cfg_b, _, base = _run_contract(time_step=time_step, strip_holdings=True, mer=0.0)
    _, _, variant = _run_contract(time_step=time_step, strip_holdings=True, mer=0.02)
    diff = base[0].non_reg_balance - variant[0].non_reg_balance
    assert 0.02 * _OPENING_NONREG <= diff <= 0.02 * base[0].non_reg_balance, diff


@pytest.mark.parametrize("time_step", _TIME_STEPS)
def test_declared_zero_mer_is_byte_identical_to_absent(time_step):
    """I7 (DP#32): ``mer: 0.0`` is a declared fee-free fact; it reaches the
    config and subtracts nothing -- the series is byte-identical to an account
    that declares no MER at all."""
    cfg_zero, _, zero = _run_contract(time_step=time_step, mer=0.0)
    cfg_abs, _, absent = _run_contract(time_step=time_step, mer=_ABSENT)
    assert cfg_zero.account_mer_drag["non_reg"]["mer_rate"] == 0.0
    assert "non_reg" not in cfg_abs.account_mer_drag
    assert [r.non_reg_balance for r in zero] == [r.non_reg_balance for r in absent]


# ── (b) fold level: a declared non_reg expected_return shifts the balance ─────

@pytest.mark.parametrize("time_step", _TIME_STEPS)
def test_declared_non_reg_expected_return_shifts_year1_balance_through_fold(time_step):
    cfg_b, sim_b, base = _run_contract(time_step=time_step, expected_return=0.05)
    cfg_v, sim_v, variant = _run_contract(time_step=time_step, expected_return=0.09)

    assert cfg_b.account_return_overrides["non_reg"]["override_balance"] == _OPENING_NONREG
    assert cfg_v.account_return_overrides["non_reg"]["override_balance"] == _OPENING_NONREG
    assert "non_reg" not in cfg_v.account_mer_drag
    _assert_on_after_tax_path(cfg_v, sim_v, variant)

    diff = variant[0].non_reg_balance - base[0].non_reg_balance
    # The blend's weighted_rate_sum differs by exactly 0.04 * override_balance,
    # and the difference flows through deferred appreciation untaxed, so the
    # year-1 gap is exactly $3,400. Before #291 this was exactly 0.0.
    assert diff == pytest.approx(0.04 * _OPENING_NONREG, rel=1e-6)

    gap_last = variant[-1].non_reg_balance - base[-1].non_reg_balance
    assert gap_last > diff

    _assert_registered_and_acb_untouched(base, variant)
    _assert_money_invariants(variant)


# ── (c) golden: absence of both inputs is a byte-identical no-op ──────────────

def test_golden_unchanged_when_non_reg_mer_and_expected_return_absent():
    from test_golden_trajectory_581 import _run, golden_household_config

    golden = golden_household_config()
    cfg = SimulationConfig.from_dict(copy.deepcopy(golden))
    assert "non_reg" not in cfg.account_mer_drag
    assert "non_reg" not in cfg.account_return_overrides
    assert _run(golden)[-1].total_assets == 9709753.139463063


# ── (d) rule level: the registered rules with a NON-None after-tax rate ──────

def _config(**overrides):
    defaults = dict(
        projection_years=5, investment_return=0.07, mortgage_balance=0,
        mortgage_rate=0.05, margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _ctx(config, *, atr, investment_return=0.07, use_readvanceable=True):
    return RuleContext(
        year=0, calendar_year=2026, allocations={}, config=config,
        investment_return=investment_return, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=use_readvanceable, deduct_later=False,
        primary_marginal_rate=0.40, spouse_marginal_rate=0.0,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=atr, cpp_income=0.0, oas_income=0.0,
        pension_income=0.0, drawdown_order=None, rrif_min_rate_primary=0.0,
        rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.0,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        living_costs=0.0, after_tax_income=0.0,
    )


_OVERRIDE_9PCT = {'non_reg': {'override_balance': 100_000.0, 'weighted_rate_sum': 9_000.0}}


class TestNonRegGrowthRuleWithAfterTaxRate:
    """The fold always hands ``apply_non_reg_growth`` a non-None after-tax rate
    (0.062 = 7% gross, 40% marginal, 2% interest yield). Each case also grows a
    $50,000 SM sleeve, which must stay on the UNSHIFTED shared rate."""

    @pytest.mark.parametrize("label,cfg_kwargs,atr,expected_nonreg", [
        ("no_declaration", {}, 0.062, 106_200.0),
        ("mer_2pct", {'account_mer_drag': {'non_reg': {'mer_rate': 0.02}}}, 0.062, 104_200.0),
        ("mer_explicit_zero", {'account_mer_drag': {'non_reg': {'mer_rate': 0.0}}}, 0.062, 106_200.0),
        ("override_9pct", {'account_return_overrides': _OVERRIDE_9PCT}, 0.062, 108_200.0),
        ("override_9pct_plus_mer_2pct",
         {'account_return_overrides': _OVERRIDE_9PCT,
          'account_mer_drag': {'non_reg': {'mer_rate': 0.02}}}, 0.062, 106_200.0),
        # Coincident case: atr == investment_return. The old gate read the fee
        # only here; it must be subtracted exactly once.
        ("coincident_mer_2pct", {'account_mer_drag': {'non_reg': {'mer_rate': 0.02}}},
         0.07, 105_000.0),
    ])
    def test_non_reg_pot_and_sm_sleeve(self, label, cfg_kwargs, atr, expected_nonreg):
        ws = YearWorkingState(year=0)
        ws.new_nonreg_bal = 100_000.0
        ws.new_sm_investment = 50_000.0
        ctx = _ctx(_config(**cfg_kwargs), atr=atr)
        RULES['non_reg_growth'](ws, ctx)
        RULES['sm_investment_growth'](ws, ctx)
        assert ws.new_nonreg_bal == pytest.approx(expected_nonreg, rel=1e-12), label
        # I10: the SM sleeve never inherits the non_reg declaration, and never
        # silently grows at a 0.0 default rate.
        assert ws.new_sm_investment == pytest.approx(50_000.0 * (1 + atr), rel=1e-12), label

    def test_explicit_zero_mer_is_byte_identical_to_absent(self):
        ws_abs = YearWorkingState(year=0)
        ws_abs.new_nonreg_bal = 100_000.0
        ws_zero = YearWorkingState(year=0)
        ws_zero.new_nonreg_bal = 100_000.0
        RULES['non_reg_growth'](ws_abs, _ctx(_config(), atr=0.062))
        RULES['non_reg_growth'](ws_zero, _ctx(
            _config(account_mer_drag={'non_reg': {'mer_rate': 0.0}}), atr=0.062))
        assert ws_zero.new_nonreg_bal == ws_abs.new_nonreg_bal


def test_mer_entry_without_rate_is_loud():
    """I8 (DP#32): a non_reg MER entry with no ``mer_rate`` is malformed input,
    not a 0% fee -- it must raise rather than silently read as fee-free."""
    ws = YearWorkingState(year=0)
    ws.new_nonreg_bal = 100_000.0
    ctx = _ctx(_config(account_mer_drag={'non_reg': {}}), atr=0.062)
    with pytest.raises(KeyError):
        RULES['non_reg_growth'](ws, ctx)
