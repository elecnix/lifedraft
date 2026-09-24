"""Issue #290: net_benefit's terminal registered tax is priced from the
household's own data, through the estate path, and is never silently zero.

Before #290 the RRSP leg of the default objective (``max_net_benefit``)
re-projected a retirement drawdown on hidden constants (a $60k expense key no
contract could set, ages from a hardcoded 2026, ``max(age + 10, 65)``
retirement, 65 CPP/OAS starts, the couple's RRSPs on the primary's brackets)
and then summed a key its rows never carried -- so for every household with a
birth date it priced the terminal RRSP as TAX-FREE.

#290 retires that leg: ``compute_net_benefit`` now subtracts the registered
deemed-disposition tax from the SAME ``compute_estate`` call
``max_after_tax_estate`` makes (DP#9, one spelling). These tests drive the
engine (``FamilySimulation.run`` via the golden fixture) and assert against
the production estate spelling or against differentials between engine runs --
never a hand-written bracket formula (DP#11).

The ask's "spending_target 40k vs 120k" test is NOT here on purpose: the
re-projection that could have consumed a spending target is deleted, so
compute_net_benefit reads no retirement spending target and there is nothing
for that pair to measure.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import objective  # noqa: E402
import net_benefit_legs  # noqa: E402
from simulation_config import SimulationConfig  # noqa: E402
from test_golden_trajectory_581 import golden_household_config, _run  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def golden():
    """(results, ocfg) for the golden household. The objective cfg is built
    exactly as the ranking path builds it (``objective_cfg``); the raw golden
    dict carries no ``tax.start_year`` and would (correctly) be refused."""
    cfg = golden_household_config()
    results = _run(cfg)
    ocfg = objective.objective_cfg(SimulationConfig.from_dict(cfg))
    return results, ocfg


def _with_estate(ocfg, estate):
    return {**ocfg, 'estate': estate}


def _with_start_year(ocfg, year):
    return {**ocfg, 'tax': {**ocfg['tax'], 'start_year': year}}


# ── the silent zero is gone ────────────────────────────────────────────────

def test_registered_leg_strictly_positive_on_golden(golden):
    """The regression the maintainer asked for: a household with a positive
    terminal RRSP and a primary birth date pays a strictly positive terminal
    registered tax under net_benefit (pre-#290 this was exactly 0)."""
    results, ocfg = golden
    final = results[-1]
    primary = [m for m in ocfg['family']['members'] if m['role'] == 'primary'][0]
    assert primary['birth_year'] > 0
    assert final.total_rrsp > 0
    assert objective.terminal_registered_tax(results, ocfg) > 0


def test_registered_leg_is_the_estate_spelling(golden):
    """DP#9: net_benefit's registered tax IS max_after_tax_estate's."""
    results, ocfg = golden
    assert (objective.terminal_registered_tax(results, ocfg)
            == objective.compute_after_tax_estate(results, ocfg).registered_tax)


def test_net_benefit_subtracts_exactly_the_estate_registered_tax(golden, monkeypatch):
    """compute_net_benefit subtracts the WHOLE registered tax: zeroing only the
    estate's ``registered_tax`` (a spy around the real compute_estate) raises
    net_benefit by exactly that amount, and moves nothing else."""
    results, ocfg = golden
    real = objective.compute_after_tax_estate(results, ocfg)
    assert real.registered_tax > 0
    nb = objective.compute_net_benefit(results, ocfg)

    _orig = objective._compute_estate

    def _zero_registered(**kw):
        return dataclasses.replace(_orig(**kw), registered_tax=0.0)

    monkeypatch.setattr(objective, '_compute_estate', _zero_registered)
    nb_untaxed = objective.compute_net_benefit(results, ocfg)
    assert nb_untaxed - nb == pytest.approx(real.registered_tax, rel=1e-12)


def test_rollover_election_moves_net_benefit_by_exactly_the_registered_tax_delta(golden):
    """The declared spousal-rollover election now reaches net_benefit, by
    exactly the difference the estate path prices (no hand formula)."""
    results, ocfg = golden
    declined = _with_estate(ocfg, {})
    full = _with_estate(ocfg, {'registered_rolled_fraction': 1.0})
    est_declined = objective.compute_after_tax_estate(results, declined)
    est_full = objective.compute_after_tax_estate(results, full)
    assert est_full.registered_tax != est_declined.registered_tax
    delta_nb = (objective.compute_net_benefit(results, declined)
                - objective.compute_net_benefit(results, full))
    assert delta_nb == pytest.approx(
        est_full.registered_tax - est_declined.registered_tax, rel=1e-9)


# ── per-owner pricing ──────────────────────────────────────────────────────

def test_even_split_pays_less_than_one_name(golden):
    """Canada has no joint filing. The SAME terminal registered total, split
    evenly between the two spouses, pays less than when it sits in one name --
    when the rollover is declined (two terminal returns). Under a full
    spousal rollover everything lands on the survivor's return, so the two are
    equal by law: that equality documents the mechanism rather than hiding it.

    dataclasses.replace on the ENGINE-produced terminal YearResult is used only
    to hold the total fixed; the engine-run companion follows."""
    results, ocfg = golden
    final = results[-1]
    total = final.total_rrsp
    assert total > 0
    even = dataclasses.replace(final, primary_rrsp=total / 2, spouse_rrsp=total / 2,
                               spousal_rrsp=0.0)
    one_name = dataclasses.replace(final, primary_rrsp=total, spouse_rrsp=0.0,
                                   spousal_rrsp=0.0)
    r_even = results[:-1] + [even]
    r_one = results[:-1] + [one_name]

    declined = _with_estate(ocfg, {'registered_rolled_fraction': 0.0})
    tax_even = objective.terminal_registered_tax(r_even, declined)
    tax_one = objective.terminal_registered_tax(r_one, declined)
    assert tax_even < tax_one
    # and the whole difference reaches net_benefit
    assert (objective.compute_net_benefit(r_even, declined)
            - objective.compute_net_benefit(r_one, declined)
            == pytest.approx(tax_one - tax_even, rel=1e-9))

    full = _with_estate(ocfg, {'registered_rolled_fraction': 1.0})
    assert (objective.terminal_registered_tax(r_even, full)
            == pytest.approx(objective.terminal_registered_tax(r_one, full), rel=1e-12))


def _run_split(primary_rrsp, spouse_rrsp, horizon_age):
    cfg = golden_household_config()
    cfg['assumptions']['horizon_age'] = horizon_age
    members = cfg['family']['members']
    members[0]['rrsp_balance'] = primary_rrsp
    members[1]['rrsp_balance'] = spouse_rrsp
    results = _run(cfg)
    ocfg = objective.objective_cfg(SimulationConfig.from_dict(cfg))
    return results, ocfg


def test_engine_even_split_lower_effective_rate():
    """Engine-driven companion: two runs of the golden household that differ
    ONLY in how the same $450k opening RRSP is owned (225k/225k vs 450k/0).

    The horizon is set before retirement (primary age 60) so no RRIF drawdown
    re-shapes the two paths; the terminal registered gross is then the same in
    both runs and the only difference the leg can see is ownership. Measured
    over the full golden horizon (age 95) the two drawdown paths diverge and
    the terminal balances end up owned in different proportions, so that
    comparison is not a clean ownership test."""
    r_even, o_even = _run_split(225_000, 225_000, horizon_age=60)
    r_one, o_one = _run_split(450_000, 0, horizon_age=60)

    def _gross(f):
        return f.total_rrsp + f.lif_balance + f.lira_balance

    g_even, g_one = _gross(r_even[-1]), _gross(r_one[-1])
    assert g_even > 0 and g_one > 0
    rate_even = objective.terminal_registered_tax(r_even, o_even) / g_even
    rate_one = objective.terminal_registered_tax(r_one, o_one) / g_one
    assert rate_even < rate_one
    # The ranking consequence: the even split scores higher under net_benefit.
    assert (objective.compute_net_benefit(r_even, o_even)
            > objective.compute_net_benefit(r_one, o_one))


# ── dates follow the document, not 2026 ────────────────────────────────────

def test_leg_follows_start_year_not_2026(golden):
    results, ocfg = golden
    o26 = _with_start_year(ocfg, 2026)
    o31 = _with_start_year(ocfg, 2031)
    t26 = objective.terminal_registered_tax(results, o26)
    t31 = objective.terminal_registered_tax(results, o31)
    assert t26 != t31
    assert t26 == objective.compute_after_tax_estate(results, o26).registered_tax
    assert t31 == objective.compute_after_tax_estate(results, o31).registered_tax
    assert (objective.compute_net_benefit(results, o26)
            - objective.compute_net_benefit(results, o31)
            == pytest.approx(t31 - t26, rel=1e-9))


def test_objective_cfg_start_year_follows_the_documents_as_of():
    """Contract pair: the same loadable document with ``as_of`` four years
    apart gives an objective cfg whose tax.start_year is that as_of's year."""
    import input_contract as ic
    from _example_doc import minimal_example

    base = minimal_example()
    seen = []
    for shift in (0, 4):
        doc = copy.deepcopy(base)
        year = int(doc['as_of'][:4]) + shift
        doc['as_of'] = f"{year}{doc['as_of'][4:]}"
        config = SimulationConfig.from_dict(ic.to_internal_config(doc))
        ocfg = objective.objective_cfg(config)
        assert ocfg['tax']['start_year'] == int(doc['as_of'][:4])
        seen.append(ocfg['tax']['start_year'])
    assert seen[1] - seen[0] == 4


def test_objective_cfg_is_the_ranking_cfg():
    """One spelling (DP#9): the keys and values the ranking path used to build
    inline, now from objective_cfg."""
    config = SimulationConfig.from_dict(golden_household_config())
    ocfg = objective.objective_cfg(config)
    assert set(ocfg) == {'assumptions', 'family', 'property', 'tax', 'estate'}
    assert ocfg['assumptions'] == {
        'capital_gains_inclusion': config.capital_gains_inclusion,
        'resp_eap_taxable_portion': config.resp_eap_taxable_portion,
        'resp_eap_tax_rate': config.resp_eap_tax_rate,
    }
    assert ocfg['family'] == {'members': config.family_members}
    assert ocfg['property'] == {'house_value': config.house_value}
    assert ocfg['tax'] == {'province': config.province, 'start_year': config.start_year}
    assert ocfg['estate'] is config.estate_data


def test_optimize_uses_objective_cfg_not_an_inline_literal():
    src = _read('optimize.py')
    assert 'cfg_dict = objective_cfg(config)' in src
    assert "'resp_eap_tax_rate': config.resp_eap_tax_rate" not in src


# ── refusals: never a plausible number from absent data ────────────────────

def test_refuses_total_rrsp_without_per_owner_split(golden):
    results, ocfg = golden
    final = results[-1]
    only_total = dataclasses.replace(final, primary_rrsp=0.0, spouse_rrsp=0.0,
                                     spousal_rrsp=0.0)
    assert only_total.total_rrsp > 0
    with pytest.raises(ValueError, match='total_rrsp'):
        objective.compute_net_benefit(results[:-1] + [only_total], ocfg)


def test_refuses_registered_with_no_non_reg_acb(golden):
    results, ocfg = golden
    no_acb = dataclasses.replace(results[-1], non_reg_acb=None)
    with pytest.raises(ValueError, match='non_reg_acb'):
        objective.compute_net_benefit(results[:-1] + [no_acb], ocfg)


@pytest.mark.parametrize('tax', [
    None,                                   # no tax block at all
    {'start_year': 2026},                   # no province
    {'province': 'quebec'},                 # no start_year / year
])
def test_refuses_registered_without_terminal_bracket_context(golden, tax):
    results, ocfg = golden
    cfg = {k: v for k, v in ocfg.items() if k != 'tax'}
    # oas_annual declared so the refusal under test is the registered leg's,
    # not the OAS fallback's.
    cfg['assumptions'] = {**cfg['assumptions'], 'oas_annual': 8_000}
    if tax is not None:
        cfg['tax'] = tax
    with pytest.raises(ValueError, match='terminal-year brackets'):
        objective.compute_net_benefit(results, cfg)


def test_explicit_tax_year_is_accepted(golden):
    """``tax.year`` (an explicit terminal calendar year) is the estate path's
    own override and satisfies the bracket context without start_year."""
    results, ocfg = golden
    cfg = {**ocfg, 'tax': {'province': ocfg['tax']['province'], 'year': 2060},
           'assumptions': {**ocfg['assumptions'], 'oas_annual': 8_000}}
    assert (objective.terminal_registered_tax(results, cfg)
            == objective.compute_after_tax_estate(results, cfg).registered_tax)


def test_default_oas_annual_refuses_without_start_year():
    with pytest.raises(ValueError, match=r"cfg\['tax'\]\['start_year'\]"):
        net_benefit_legs._default_oas_annual({'assumptions': {}})
    with pytest.raises(ValueError, match=r"cfg\['tax'\]\['start_year'\]"):
        net_benefit_legs._default_oas_annual({'tax': {'province': 'quebec'}})


def test_lsif_refuses_without_start_year_but_absent_block_needs_no_year():
    lsif_block = {'purchase_amount': 5_000, 'purchase_year': 2026}
    with pytest.raises(ValueError, match=r"cfg\['tax'\]\['start_year'\]"):
        net_benefit_legs.lsif_credit_total({'lsif': lsif_block})
    # DP#16 absence: no block (or an empty one) prices nothing and needs no year.
    assert net_benefit_legs.lsif_credit_total({}) == 0.0
    assert net_benefit_legs.lsif_credit_total({'lsif': {}}) == 0.0
    # With the household's year it prices, and the year it uses is the cfg's.
    with_year = net_benefit_legs.lsif_credit_total(
        {'lsif': lsif_block, 'tax': {'start_year': 2026}})
    assert with_year > 0


def test_zero_registered_and_no_sleeve_skips_the_estate(golden, monkeypatch):
    """No registered dollar and no SM sleeve: nothing is included in income,
    a modelled 0.0, and compute_estate is never called (instrumented)."""
    results, ocfg = golden
    empty = dataclasses.replace(
        results[-1], primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_rrsp=0.0, lif_balance=0.0, lira_balance=0.0,
        sm_investment_balance=0.0)
    calls = []

    def _spy(**kw):
        calls.append(kw)
        raise AssertionError('compute_estate must not be called')

    monkeypatch.setattr(objective, '_compute_estate', _spy)
    r = results[:-1] + [empty]
    assert objective.terminal_registered_tax(r, ocfg) == 0.0
    objective.compute_net_benefit(r, ocfg)
    assert calls == []
    assert objective.terminal_registered_tax([], ocfg) == 0.0


def test_lif_alone_triggers_the_estate(golden):
    """A LIF/LIRA-only registered balance is priced too (not just RRSPs)."""
    results, ocfg = golden
    lif_only = dataclasses.replace(
        results[-1], primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_rrsp=0.0, lif_balance=200_000.0, lira_balance=0.0,
        sm_investment_balance=0.0)
    r = results[:-1] + [lif_only]
    tax = objective.terminal_registered_tax(r, ocfg)
    assert tax > 0
    assert tax == objective.compute_after_tax_estate(r, ocfg).registered_tax


# ── the D11 stash still serves the registered leg, identity-keyed ─────────

def test_stash_is_identity_keyed_for_the_registered_leg(golden):
    """A stash keyed to a DIFFERENT results list is ignored; the right one is
    reused (no second compute_estate)."""
    results, ocfg = golden
    real = objective.compute_after_tax_estate(results, ocfg)
    poisoned = dataclasses.replace(real, registered_tax=real.registered_tax + 12_345.0)

    wrong = {**ocfg, '_precomputed_estate_result': poisoned,
             '_precomputed_estate_for': list(results)}   # equal, not identical
    assert objective.terminal_registered_tax(results, wrong) == real.registered_tax

    right = {**ocfg, '_precomputed_estate_result': poisoned,
             '_precomputed_estate_for': results}
    assert objective.terminal_registered_tax(results, right) == poisoned.registered_tax


def test_ranking_path_computes_the_estate_once(monkeypatch):
    """evaluate_strategy_with_simulation calls compute_estate exactly once per
    strategy: the objective's registered + SM legs and both report columns
    reuse the stash."""
    import optimize
    from countries.canada.strategies import STRATEGY_BALANCED

    calls = []
    _orig_opt = optimize.compute_estate
    _orig_obj = objective._compute_estate

    def _count_opt(**kw):
        calls.append('optimize')
        return _orig_opt(**kw)

    def _count_obj(**kw):
        calls.append('objective')
        return _orig_obj(**kw)

    monkeypatch.setattr(optimize, 'compute_estate', _count_opt)
    monkeypatch.setattr(objective, '_compute_estate', _count_obj)
    cfg = golden_household_config()
    cfg['assumptions']['horizon_age'] = 60
    config = SimulationConfig.from_dict(cfg)
    from countries.canada.rate_model import build_rate_path
    rp = build_rate_path('test', 0.05, config.projection_years, 'variable', [0.05])
    out = optimize.evaluate_strategy_with_simulation(
        name='once', strategy=STRATEGY_BALANCED, config=config, rate_path=rp,
        use_readvanceable=False, objective=objective.MAX_NET_BENEFIT,
        include_year_by_year=False)
    assert calls == ['optimize']
    import math
    assert math.isfinite(out['net_benefit'])


# ── every optimizer mode scores with the household's own cfg ───────────────

def _spy_objective(seen):
    def fn(results, cfg):
        seen.append(cfg)
        return objective.compute_net_benefit(results, cfg)
    return objective.ObjectiveFunction(name='max_net_benefit_spy', fn=fn)


def _rrsp_config():
    """Fabricated round-number couple WITH registered balances, so the
    registered leg (and its refusals) is live in every optimizer mode."""
    return SimulationConfig(
        projection_years=3, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000, 'birth_year': 1980,
             'rrsp_balance': 100000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1982,
             'rrsp_balance': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
        mortgage_balance=100000, mortgage_rate=0.05, house_value=400000,
        refinance_amortization_years=25,
        province='quebec', start_year=2030,
    )


def _assert_every_cfg_is_the_households(seen, config):
    assert seen, 'the objective was never evaluated'
    expected = objective.objective_cfg(config)
    for cfg in seen:
        assert cfg == expected
        assert cfg['tax'] == {'province': 'quebec', 'start_year': 2030}


def test_grid_optimizer_scores_with_objective_cfg():
    import math
    from optimizer import GridOptimizer
    from countries.canada.strategies import STRATEGY_BALANCED
    config = _rrsp_config()
    seen = []
    ranked = GridOptimizer(config).optimize(
        strategies=[STRATEGY_BALANCED], objective=_spy_objective(seen),
        use_readvanceable_options=[False], deduct_later_options=[False],
        ltv_levels=[0.0], income_overrides=[None])
    _assert_every_cfg_is_the_households(seen, config)
    assert ranked and all(math.isfinite(r.score) for r in ranked)


def test_scipy_optimizer_scores_with_objective_cfg():
    """scipy's candidate loop swallows every exception into -inf, so an arity
    guard alone cannot see a wrong cfg here -- the spy and a finite best score
    can."""
    import math
    from scipy_optimizer import ScipyOptimizer
    from countries.canada.strategies import STRATEGY_BALANCED
    config = _rrsp_config()
    seen = []
    out = ScipyOptimizer(config, optimize_vars=['ltv']).optimize(
        strategies=[STRATEGY_BALANCED], objective=_spy_objective(seen))
    _assert_every_cfg_is_the_households(seen, config)
    assert math.isfinite(out[0].score)


def test_monte_carlo_optimizer_scores_with_objective_cfg():
    import math
    from monte_carlo_optimizer import MonteCarloOptimizer
    from countries.canada.strategies import STRATEGY_BALANCED
    config = _rrsp_config()
    seen = []
    ranked = MonteCarloOptimizer(config, n_simulations=2).optimize(
        strategies=[STRATEGY_BALANCED], objective=_spy_objective(seen),
        use_readvanceable_options=[False], deduct_later_options=[False])
    _assert_every_cfg_is_the_households(seen, config)
    assert ranked and all(math.isfinite(r.score) for r in ranked)


@pytest.mark.parametrize('decision_type,lookahead', [
    ('baseline', 0),        # the non-deduct-later branch
    ('deduct_later', 0),    # greedy claim-fraction branch (inside except)
    ('deduct_later', 1),    # lookahead branch
])
def test_dp_optimizer_scores_with_objective_cfg(decision_type, lookahead):
    import math
    from dataclasses import replace
    from dp_optimizer import (
        DPOptimizer, Decision, DECISION_DEDUCT_LATER, DECISION_DRAWDOWN_ORDER,
    )
    from countries.canada.strategies import STRATEGY_BALANCED
    config = _rrsp_config()
    seen = []
    dtype = DECISION_DEDUCT_LATER if decision_type == 'deduct_later' else DECISION_DRAWDOWN_ORDER
    decision = Decision(name=decision_type, decision_type=dtype)
    out = DPOptimizer(config, lookahead=lookahead).optimize(
        strategies=[STRATEGY_BALANCED], objective=_spy_objective(seen),
        decision_class=decision)
    # _optimize_deduct_later hands each candidate a replace()d config that
    # differs only in deduct_later_bracket_target -- objective_cfg ignores it.
    _assert_every_cfg_is_the_households(seen, config)
    assert math.isfinite(out[0].total_score)
    for step in out[0].decision_path:
        assert math.isfinite(step.score_contribution)


# ── static guards: the hidden constants cannot come back ───────────────────

def _read(name):
    with open(os.path.join(_ROOT, name), encoding='utf-8') as f:
        return f.read()


def test_static_no_hidden_constants_in_net_benefit_legs():
    src = _read('net_benefit_legs.py')
    tree = ast.parse(src)
    year_literals = [
        (n.lineno, n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and type(n.value) is int
        and 1900 <= n.value <= 2100
    ]
    assert year_literals == [], (
        f"calendar-year literals in net_benefit_legs.py: {year_literals} -- "
        "read cfg['tax']['start_year'] via _tax_start_year (issue #290)")
    strings = {n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert 'retirement_expenses' not in strings
    assert 'tax_owed' not in strings
    assert ".get('retirement_expenses'" not in src
    assert '.get("retirement_expenses"' not in src
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for retired in ('project_retirement', 'RetirementState', 'rrsp_withdrawal_tax',
                    '_CURRENT_YEAR'):
        assert retired not in names, f"{retired} is back in net_benefit_legs.py"


def test_static_retired_leg_absent_from_objective():
    tree = ast.parse(_read('objective.py'))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
              for a in n.names}
    for retired in ('project_retirement', 'RetirementState', 'rrsp_withdrawal_tax',
                    '_rrsp_withdrawal_tax'):
        assert retired not in names


@pytest.mark.parametrize('module', ['objective.py', 'model_fidelity.py'])
def test_static_no_flat_30_percent_disclosure(module):
    src = _read(module).lower()
    for phrase in ('flat 30%', '30% withdrawal', 'simplified 30%'):
        assert phrase not in src, f"{module} still says {phrase!r}"


def test_disclosure_names_what_remains():
    import model_fidelity
    by_id = {a.id: a for a in model_fidelity.all_approximations()}
    assert 'net_benefit_withdrawal_tax_is_estimated' not in by_id
    caveat = by_id['net_benefit_registered_tax_at_horizon']
    assert caveat.issue == '#290'
    assert caveat.direction == model_fidelity.Direction.UNKNOWN
    text = (caveat.summary + ' ' + caveat.detail).lower()
    for phrase in ('deemed disposition', 'horizon', 'tax.start_year + len(results) - 1',
                   'tax.province', 'fhsa', 'primary', '_undeclared_estate_defaults'):
        assert phrase in text, phrase
    # LIF / LIRA are attributed to the PRIMARY (objective._estate_call_args);
    # the disclosure must say so rather than claim true per-owner locked-in.
    assert 'lif and lira balances are attributed wholly to the primary' in text


# ── the console disclosure beside the estate ranking ───────────────────────

def test_estate_ranking_console_text_describes_what_net_benefit_prices(capsys):
    """optimize._print_estate_ranking prints the max_after_tax_estate ranking
    beside the net_benefit one. Its preamble must describe net_benefit as it
    is after #290 (it prices the registered rollover) -- not the retired
    "ESTIMATED pre-death withdrawal tax" -- and it must flag when the two
    objectives disagree on the top strategy."""
    import optimize
    results = [
        {'strategy': 'a', 'deduct_later': False, 'net_benefit': 900_000,
         'after_tax_estate': 700_000},
        {'strategy': 'b', 'deduct_later': True, 'net_benefit': 800_000,
         'after_tax_estate': 750_000},
    ]
    results_sorted = sorted(results, key=lambda r: r['net_benefit'], reverse=True)
    optimize._print_estate_ranking(results_sorted, results)
    out = capsys.readouterr().out
    assert 'ESTIMATED pre-death withdrawal tax' not in out
    assert "prices your registered plans' rollover" in out
    assert 'THE TWO OBJECTIVES DISAGREE' in out
