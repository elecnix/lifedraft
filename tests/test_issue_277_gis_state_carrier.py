"""Issue #277 (slice 1): the prior year's GIS-countable income is carried by
``SimState``, not threaded by the caller, so EVERY fold pays GIS.

Before #277, CRA's prior-year GIS income test was fed from outside the pure
step: ``FamilySimulation.run()`` and ``_run_monthly`` computed the value from
the previous ``YearResult`` and passed it in, while ``Optimizer._run_simulation``
(Grid / Scipy / Monte-Carlo) and ``DPOptimizer`` called
``simulate_year(state, year, ctx)`` without it. The optimizer folds therefore
paid $0 GIS to a GIS-eligible household that ``run()`` paid GIS to (measured on
``_modest_gis_household``: run 702.1875 vs grid 0.0; terminals
216492.56702473873 vs 215165.03062572738). The #627 parity test did not catch
it because its fixtures never qualify for GIS.

Now ``simulate_year_pure`` writes this year's countable income into
``jurisdiction_state['canada']`` and reads last year's back from the opening
state, so the value travels with ``SimState`` through any fold.

Every parity assertion here first asserts the household actually receives GIS
on BOTH paths (non-vacuity): two folds that agree on $0 GIS are the bug, not a
pass. The year-0 assertions use a household that is ALREADY retired and 65+ at
the start, because only for such a household does "no prior year" coerced to
$0 visibly pay full GIS in year 0 (the default modest household is 55 at the
start, so a year-0 check on it would be vacuous).

Fixtures are fabricated (DP#4/DP#15): role names, round numbers.

These tests drive the engine (``FamilySimulation.run``,
``GridOptimizer._run_simulation``, ``DPOptimizer.optimize``, ``simulate_year``)
and derive expected GIS from ``gis_benefit`` applied to ``YearResult`` fields,
never from hand-built engine state (DP#11/DP#18). The only direct call below
the engine is the unit test of the pure helper ``_carried_gis_countable_income``;
the malformed-value and 0.0-vs-None tests start from an ENGINE-produced state
and change only the carried key.
"""
from __future__ import annotations

import copy
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

import simulation_state
from countries.canada.adapter import CanadaAdapter
from countries.canada.retirement import gis_benefit
from optimizer import GridOptimizer
from simulation import FamilySimulation, simulate_year
from simulation_config import SimulationConfig

from test_s04_gis_maximization import _modest_gis_household

# ``GIS_COUNTABLE_INCOME_KEY`` (added by #277) is imported inside each test
# that needs it, not here: on the pre-#277 engine the fold tests below then
# FAIL BY ASSERTION (grid GIS 0.0 vs run() 702.1875) instead of the whole
# module erroring at import, which would prove nothing about the folds.


# ── fixtures ────────────────────────────────────────────────────────────────

def _modest_cfg(**assumptions) -> SimulationConfig:
    d = _modest_gis_household()
    d['assumptions'].update(assumptions)
    return SimulationConfig.from_dict(d)


def _retired_at_start_dict(**assumptions) -> dict:
    """The modest household, but the primary is ALREADY 66 and retired at the
    start (birth 1960, retired at 65, no salary). Year 0 has no prior year in
    the projection, so GIS must be 0.0 there; year 1's prior year is a low
    retirement year, so GIS must be > 0 from year 1. Coercing "no prior year"
    to $0 of countable income would pay FULL GIS in year 0 -- this fixture is
    what makes that mistake visible."""
    d = _modest_gis_household()
    d['family']['members'][0].update(
        birth_year=1960, retirement_age=65, gross_income=0)
    d['assumptions'].update(assumptions)
    return d


def _sim(cfg: SimulationConfig, **kw) -> FamilySimulation:
    return FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                            use_readvanceable=False, deduct_later=False, **kw)


def _gis_sum(results) -> float:
    return sum(r.gis_income for r in results)


def _dp_all_results(cfg: SimulationConfig):
    """DPOptimizer's per-year fork, with a decision class whose every year
    takes the 'baseline' branch (simulate_year with deduct_later=True)."""
    from dp_optimizer import DECISION_DRAWDOWN_ORDER, Decision, DPOptimizer

    strategy = _sim(cfg).strategy
    (dp,) = DPOptimizer(cfg).optimize(
        strategies=[strategy],
        decision_class=Decision(name="drawdown_order",
                                decision_type=DECISION_DRAWDOWN_ORDER))
    assert [s.action_name for s in dp.decision_path] == (
        ["baseline"] * cfg.projection_years)
    return dp.all_results


def _canada(state) -> dict:
    return state.jurisdiction_state['canada']


def _with_carried(state, value):
    """``state`` with ONLY the carried GIS-countable income replaced (fresh
    dicts; the engine-produced state is not mutated)."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    canada = dict(_canada(state))
    canada[GIS_COUNTABLE_INCOME_KEY] = value
    return dataclasses.replace(
        state, jurisdiction_state={**state.jurisdiction_state, 'canada': canada})


def _countable(r) -> float:
    # The CRA prior-year income test base, spelled from the RESULT (not from
    # engine internals): everything the household received EXCEPT OAS and GIS.
    return r.retirement_income + r.employment_income - r.oas_income - r.gis_income


# ── the fix: every fold pays GIS the same way ──────────────────────────────

def test_grid_fold_matches_run_on_gis_household():
    """GridOptimizer._run_simulation reproduces FamilySimulation.run() year
    for year on a GIS-eligible household. Fails on the pre-#277 engine (grid
    paid 0.0 GIS; trajectories diverged from year 11)."""
    cfg = _modest_cfg()
    sim = _sim(cfg)
    strategy = sim.strategy
    run_results = sim.run()
    grid_results, _ = GridOptimizer(cfg)._run_simulation(
        cfg, strategy, use_readvanceable=False, deduct_later=False, lump_sum=0.0)

    # Non-vacuity FIRST, on both sides: agreeing on $0 GIS is the bug.
    assert _gis_sum(run_results) > 0, "run() must pay GIS on this household"
    assert _gis_sum(grid_results) > 0, (
        f"GridOptimizer fold paid {_gis_sum(grid_results)!r} GIS where run() "
        f"paid {_gis_sum(run_results)!r} -- the prior-year GIS income is not "
        f"reaching the optimizer fold")

    assert len(run_results) == len(grid_results)
    for year, (a, b) in enumerate(zip(run_results, grid_results)):
        assert dataclasses.asdict(a) == dataclasses.asdict(b), (
            f"year {year}: optimizer fold diverged from run() "
            f"(gis {b.gis_income!r} vs {a.gis_income!r})")
    assert grid_results[-1].total_assets == run_results[-1].total_assets
    # Pinned exactly, so run() itself cannot drift unnoticed: these are the
    # values run() produced before #277 (measured on origin/main 594b6f8).
    assert _gis_sum(run_results) == 702.1875
    assert _gis_sum(grid_results) == 702.1875
    assert run_results[-1].total_assets == 216492.56702473873
    assert grid_results[-1].total_assets == 216492.56702473873


def test_grid_fold_pays_gis_per_the_rule():
    """Rule-pinned, not parity-pinned (#627: parity alone proves nothing if
    both folds are wrong). On the Grid path's OWN output: year 0 has no prior
    year -> 0.0; every retired year i >= 1 pays gis_benefit() of year i-1's
    countable income -- including the first retired year, whose prior year
    was a working year with salary."""
    cfg = _modest_cfg()
    strategy = _sim(cfg).strategy
    results, _ = GridOptimizer(cfg)._run_simulation(
        cfg, strategy, use_readvanceable=False, deduct_later=False, lump_sum=0.0)

    assert results[0].gis_income == 0.0
    checked = 0
    for i in range(1, len(results)):
        r, prior = results[i], results[i - 1]
        if not r.any_retired:
            assert r.gis_income == 0.0, f"year {r.year}: GIS before retirement"
            continue
        cal = cfg.start_year + r.year - 1
        expected = gis_benefit(_countable(prior), is_coupled=False,
                               year=cal)['gis_amount']
        assert r.gis_income == expected, (
            f"year {r.year}: grid GIS {r.gis_income!r} != rule {expected!r}")
        checked += 1
    assert checked > 0, "no retired year was checked"
    assert _gis_sum(results) > 0, "the rule check must not be vacuous"


def test_dp_optimizer_baseline_fold_pays_gis():
    """DPOptimizer's per-year fork carries the value through SimState too.

    A non-deduct_later decision class sends every year through DP's
    'baseline' branch (simulate_year with deduct_later=True), which must
    equal Grid with deduct_later=True on GIS, year for year."""
    cfg = _modest_cfg()
    strategy = _sim(cfg).strategy
    dp_results = _dp_all_results(cfg)
    grid_results, _ = GridOptimizer(cfg)._run_simulation(
        cfg, strategy, use_readvanceable=False, deduct_later=True, lump_sum=0.0)

    assert _gis_sum(dp_results) > 0, "DP fold must pay GIS on this household"
    assert _gis_sum(grid_results) > 0
    assert [r.gis_income for r in dp_results] == [
        r.gis_income for r in grid_results]


# ── DP#32: year 0 is a loud "no prior year", on every fold ─────────────────

@pytest.mark.parametrize("time_step,kw", [
    ('annual', {}),
    ('monthly', {}),
    ('monthly', {'lump_sum': 10_000.0}),
    ('monthly', {'free_cash': 10_000.0}),
], ids=['annual', 'monthly', 'monthly-lump-sum', 'monthly-free-cash'])
def test_year0_has_no_prior_year_on_every_fold(time_step, kw):
    """A household already retired at 66: no GIS in year 0 (no prior year in
    the projection -- never coerced to $0 income, which would pay full GIS),
    GIS from year 1."""
    cfg = SimulationConfig.from_dict(_retired_at_start_dict(time_step=time_step))
    results = _sim(cfg, **kw).run()
    assert results[0].any_retired, "fixture must be retired in year 0"
    assert results[0].gis_income == 0.0, (
        f"year 0 paid {results[0].gis_income!r} GIS with no prior year")
    assert results[1].gis_income > 0, "non-vacuity: GIS must start in year 1"


def test_monthly_lump_sum_prestep_does_not_seed_year0_gis():
    """_run_monthly runs a year-0 lump-sum PRE-step before the real year-0
    step. That pre-step writes a countable income into the state it returns;
    the real year-0 step must still see "no prior year", not the pre-step's
    value (which would pay GIS in year 0)."""
    cfg = SimulationConfig.from_dict(_retired_at_start_dict(time_step='monthly'))
    results = _sim(cfg, lump_sum=10_000.0).run()
    assert results[0].gis_income == 0.0
    assert any(r.gis_income > 0 for r in results[1:]), "non-vacuity"


def test_grid_year0_has_no_prior_year():
    cfg = SimulationConfig.from_dict(_retired_at_start_dict())
    strategy = _sim(cfg).strategy
    results, _ = GridOptimizer(cfg)._run_simulation(
        cfg, strategy, use_readvanceable=False, deduct_later=False, lump_sum=0.0)
    assert results[0].gis_income == 0.0
    assert results[1].gis_income > 0


def test_dp_year0_has_no_prior_year():
    results = _dp_all_results(SimulationConfig.from_dict(_retired_at_start_dict()))
    assert results[0].gis_income == 0.0
    assert results[1].gis_income > 0


def test_initial_state_records_no_prior_year():
    """The engine-built opening state carries the key, set to None (no prior
    year recorded) -- never an invented $0 of income."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    cfg = SimulationConfig.from_dict(_retired_at_start_dict())
    assert _canada(_sim(cfg)._state)[GIS_COUNTABLE_INCOME_KEY] is None


# ── the carrier itself ──────────────────────────────────────────────────────

def test_closing_state_carries_gis_countable_every_year():
    """Fold simulate_year by hand with the engine's own context and check
    EVERY closing state: it carries THAT year's countable income, bit for bit
    (same operand order as the rule's base). Checked per year because the
    canada dict is shallow-copied forward -- a skipped write would carry a
    stale value, not an absent one."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    cfg = _modest_cfg()
    sim = _sim(cfg)
    state = sim._state
    assert _canada(state)[GIS_COUNTABLE_INCOME_KEY] is None, (
        "the opening state must not invent a prior year")
    for year in range(cfg.projection_years):
        opening = copy.deepcopy(_canada(state))
        prev_state = state
        result, state = simulate_year(state, year, sim._build_context())
        # DP#26 purity: the step writes only into the NEXT state's fresh
        # canada dict -- the opening dict (which a DP fork shares between
        # several candidate steps) is untouched, carry key included.
        assert _canada(prev_state) == opening, f"year {year}: input mutated"
        assert _canada(state) is not _canada(prev_state)
        stored = _canada(state)[GIS_COUNTABLE_INCOME_KEY]
        assert isinstance(stored, float)
        assert stored == _countable(result), f"year {year}"

    # And the published final state of a real run() agrees.
    sim2 = _sim(cfg)
    run_results = sim2.run()
    final = _canada(sim2._state)[GIS_COUNTABLE_INCOME_KEY]
    assert final == _countable(run_results[-1])
    assert _gis_sum(run_results) > 0


# ── every fold hands the step a carried value (INV-14) ───────────────────

def _assert_every_opening_carries_a_float(monkeypatch, fold):
    """Wrap ``simulate_year_pure`` (every fold reaches it through a
    call-time import) and assert that each step at year > 0 opens on a state
    carrying a float -- i.e. no fold path hands the step a state that lost or
    never received the prior year's value (which would silently pay $0 GIS:
    the #277 bug shape)."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    real = simulation_state.simulate_year_pure
    seen = []

    def checked(state, year, *a, **kw):
        if year > 0:
            carried = _canada(state).get(GIS_COUNTABLE_INCOME_KEY, '<absent>')
            assert isinstance(carried, float), (
                f"year {year} opened on carried {carried!r}")
            seen.append(year)
        return real(state, year, *a, **kw)

    monkeypatch.setattr(simulation_state, 'simulate_year_pure', checked)
    fold()
    assert seen, "the wrapper saw no step at year > 0 (not wired)"
    return seen


@pytest.mark.parametrize("fold", [
    'run-annual', 'run-monthly', 'run-monthly-lump-sum', 'grid', 'dp'])
def test_every_fold_opens_each_year_on_a_carried_value(monkeypatch, fold):
    time_step = 'monthly' if fold.startswith('run-monthly') else 'annual'
    cfg = SimulationConfig.from_dict(_retired_at_start_dict(time_step=time_step))

    def go():
        if fold == 'grid':
            GridOptimizer(cfg)._run_simulation(
                cfg, _sim(cfg).strategy, use_readvanceable=False,
                deduct_later=False, lump_sum=0.0)
        elif fold == 'dp':
            _dp_all_results(cfg)
        else:
            kw = {'lump_sum': 10_000.0} if fold.endswith('lump-sum') else {}
            _sim(cfg, **kw).run()

    seen = _assert_every_opening_carries_a_float(monkeypatch, go)
    assert set(range(1, cfg.projection_years)) <= set(seen)


# ── zero is a value, None is absence; malformed values are refused ─────────

def _retired_state_after_year0():
    """An ENGINE-produced opening state for year 1 (retired-at-start
    household) plus the context that produced it."""
    cfg = SimulationConfig.from_dict(_retired_at_start_dict())
    sim = _sim(cfg)
    ctx = sim._build_context()
    _, state1 = simulate_year(sim._state, 0, ctx)
    return cfg, ctx, state1


def test_carried_zero_pays_full_gis_and_none_pays_none():
    """DP#32 through the engine: a carried $0 of countable income is a real
    value (full GIS for that year), while None ("no prior year recorded") pays
    none. They must never be confused in either direction."""
    cfg, ctx, state1 = _retired_state_after_year0()
    carried_real, _ = simulate_year(state1, 1, ctx)
    at_zero, _ = simulate_year(_with_carried(state1, 0.0), 1, ctx)
    at_none, _ = simulate_year(_with_carried(state1, None), 1, ctx)

    full = gis_benefit(0.0, is_coupled=False, year=cfg.start_year + 1)['gis_amount']
    assert full > 0
    assert at_zero.gis_income == full
    assert at_zero.gis_income > carried_real.gis_income > 0
    assert at_none.gis_income == 0.0


@pytest.mark.parametrize("bad,exc", [
    (float('nan'), ValueError), (float('inf'), ValueError),
    (float('-inf'), ValueError), ('1234', TypeError), (True, TypeError),
    ([1.0], TypeError)], ids=['nan', 'inf', '-inf', 'str', 'bool', 'list'])
def test_malformed_carried_value_is_refused_by_the_step(bad, exc):
    """A malformed carried value makes the ENGINE step raise, naming the key,
    instead of flowing through gis_benefit into a plausible GIS figure."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    _, ctx, state1 = _retired_state_after_year0()
    with pytest.raises(exc, match=GIS_COUNTABLE_INCOME_KEY):
        simulate_year(_with_carried(state1, bad), 1, ctx)


def test_absent_key_after_year0_is_refused_by_the_step():
    """A canada dict that lost the key (not built by the engine) cannot say
    whether a prior year exists: the step raises rather than paying no GIS."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    _, ctx, state1 = _retired_state_after_year0()
    canada = dict(_canada(state1))
    del canada[GIS_COUNTABLE_INCOME_KEY]
    broken = dataclasses.replace(
        state1, jurisdiction_state={**state1.jurisdiction_state, 'canada': canada})
    with pytest.raises(KeyError, match=GIS_COUNTABLE_INCOME_KEY):
        simulate_year(broken, 1, ctx)


# ── the pure opening-state reader ───────────────────────────────────────────

def test_opening_reader_year0_is_none_even_with_key():
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    assert _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: 1234.5}, 0) is None
    assert _carried_gis_countable_income({}, 0) is None


def test_opening_reader_unstepped_state_is_none_not_zero():
    """None (an unstepped state) is absence -- never read as $0 of income."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    assert _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: None}, 3) is None


def test_opening_reader_absent_key_after_year0_raises():
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    with pytest.raises(KeyError, match=GIS_COUNTABLE_INCOME_KEY):
        _carried_gis_countable_income({}, 3)


def test_opening_reader_returns_stored_value():
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    assert _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: 1234.5}, 3) == 1234.5


def test_opening_reader_zero_is_a_value():
    """DP#32: a stored $0 of countable income is a real value (full GIS),
    distinct from "no prior year" (None, no GIS)."""
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    got = _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: 0.0}, 3)
    assert got == 0.0 and got is not None


@pytest.mark.parametrize("bad", ['1234', True, [1.0]])
def test_opening_reader_refuses_non_numeric(bad):
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    with pytest.raises(TypeError, match=GIS_COUNTABLE_INCOME_KEY):
        _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: bad}, 3)


@pytest.mark.parametrize("bad", [float('nan'), float('inf'), float('-inf')],
                         ids=['nan', 'inf', '-inf'])
def test_opening_reader_refuses_non_finite(bad):
    from canada_state_accessors import GIS_COUNTABLE_INCOME_KEY
    from simulation_state import _carried_gis_countable_income
    with pytest.raises(ValueError, match=GIS_COUNTABLE_INCOME_KEY):
        _carried_gis_countable_income({GIS_COUNTABLE_INCOME_KEY: bad}, 3)
