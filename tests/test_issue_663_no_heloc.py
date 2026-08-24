#!/usr/bin/env python3
"""Golden no-HELOC household (issue #663, epic #603).

A household with a ``kind=mortgage`` liability and NO ``kind=heloc``
liability is not an edge case -- it is the null hypothesis the entire Smith
Manoeuvre thesis is measured against, and nothing in CI covered it before
this file. Two bugs, both DP#32 ("zero is a value, not a fallback; absence
must fail loudly"):

1. A hard crash: ``apply_overlay()`` (and other consumers) indexed
   ``cfg['property']['margin_available']`` directly. ``input_contract.py``
   correctly omits that key when there is no HELOC liability (writing ``0``
   would be exactly the silent fallback DP#32 forbids), so any
   ``--overlay``/``--preset`` run on such a household died with a KeyError.

2. Worse than the crash: before dying, the optimizer's INPUTS header quoted
   a HELOC rate, an after-tax Smith-Manoeuvre borrowing cost, and a net
   investment spread for a strategy this household structurally cannot
   execute -- and the readvanceable-mortgage strategy could be silently
   ranked (or swallowed to a hidden -inf) instead of being reported
   unavailable.

This file locks the fix: ``has_readvanceable_facility()`` is the one
explicit predicate every consumer asks (never
``cfg.get('property', {}).get('margin_available', 0)``, which cannot tell
"no facility" apart from "a facility with zero undrawn room"); the
readvanceable/Smith-Manoeuvre strategy is refused, not ranked, when the
predicate is False; and the INPUTS header reports that refusal instead of
fabricating a rate.

DP#15: no personal data. The fixture is the shipped input-contract example
(``schema/example.json``), trimmed to the one couple + children the legacy
engine represents (same helper ``tests/test_input_contract.py`` and
``tests/test_issue_367_pandas_loud_failure.py`` use), with its ``heloc``
liability removed. All figures are the shipped example's fabricated round
numbers -- nothing from any real contract.
"""
import json
import sys

import pytest

import input_contract as ic
import optimize
import output_paths
from config_access import has_readvanceable_facility
from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig


def _no_heloc_contract() -> dict:
    """The shipped example contract, trimmed to one couple + children, with
    its ``kind=heloc`` liability removed -- a plain mortgage, no
    readvanceable line. Every other liability/account/property is untouched
    fabricated example data (DP#15)."""
    from test_input_contract import _load_example, _two_generation_subset
    doc = _two_generation_subset(_load_example())
    doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "heloc"]
    return doc


def _mapped_no_heloc_cfg() -> dict:
    """The no-HELOC contract mapped to the internal dict shape
    (``apply_overlay``/``SimulationConfig.from_dict``'s input)."""
    return ic.to_internal_config(_no_heloc_contract())


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect output_paths.output_path() into a tmp dir (DP#15: never
    write test output into the user's real cache)."""
    d = tmp_path / "cache"
    monkeypatch.setattr(output_paths, 'CACHE_DIR', str(d))
    return d


@pytest.fixture
def no_heloc_input_json(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(_no_heloc_contract()))
    return str(path)


# ── Symptom 1: the crash ────────────────────────────────────────────────

class TestNoCrash:
    """A mortgage-only contract must not raise anywhere in the pipeline."""

    def test_mapped_config_has_no_margin_available_key(self):
        """input_contract.py must keep omitting the key (not default it to
        0) when there is no HELOC -- this is the precondition every other
        assertion in this file depends on."""
        cfg = _mapped_no_heloc_cfg()
        assert 'margin_available' not in cfg['property']

    def test_has_readvanceable_facility_is_false(self):
        cfg = _mapped_no_heloc_cfg()
        assert has_readvanceable_facility(cfg) is False

    def test_has_readvanceable_facility_is_true_with_a_real_heloc(self):
        """Sanity check: the predicate is not just always False."""
        from test_input_contract import _load_example, _two_generation_subset
        cfg = ic.to_internal_config(_two_generation_subset(_load_example()))
        assert has_readvanceable_facility(cfg) is True

    def test_apply_overlay_does_not_crash(self):
        """The exact crash site from #663: apply_overlay() used to index
        cfg['property']['margin_available'] directly and raise KeyError."""
        cfg = _mapped_no_heloc_cfg()
        overlay = ScenarioOverlay(label="refinance", cash_out=50_000,
                                   mortgage_rate=cfg['property']['mortgage_rate'])
        result = apply_overlay(cfg, overlay)  # must not raise
        # The overlay must not invent the key either (DP#32): still absent.
        assert 'margin_available' not in result['property']

    def test_simulation_config_from_dict_has_heloc_false(self):
        cfg = _mapped_no_heloc_cfg()
        sim_cfg = SimulationConfig.from_dict(cfg)
        assert sim_cfg.has_heloc is False
        assert sim_cfg.is_readvanceable is False

    def test_optimize_runs_end_to_end(self, cache_dir, no_heloc_input_json, monkeypatch):
        """issue #663's headline repro: `optimize.py --input <mortgage-only
        contract>` used to crash with KeyError('margin_available'). It must
        now complete and still produce a ranked-strategies table."""
        monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', no_heloc_input_json])
        optimize.main()  # must not raise


# ── Symptom 2: the honest report ────────────────────────────────────────

class TestSmithManoeuvreReportedUnavailable:
    """The optimizer must refuse to rank readvanceable/SM strategies against
    a nonexistent facility, and say so -- not silently score them, and not
    let them sink to a hidden -inf."""

    def test_readvance_priority_not_discovered(self):
        cfg = _mapped_no_heloc_cfg()
        from strategy import FamilyState
        from countries.canada.strategies import discover_strategies
        state = FamilyState(primary_income=100_000, spouse_income=60_000,
                             primary_marginal_rate=0.30, spouse_marginal_rate=0.20)
        discovered = discover_strategies(
            state, cfg['property'], investment_return=0.07, heloc_rate=0.05,
        )
        assert not any(s.prioritize_readvanceable for s in discovered.values())

    def test_no_result_is_flagged_readvanceable(self, cache_dir, no_heloc_input_json):
        """run_optimization() must never return a candidate that claims the
        readvanceable mortgage strategy when has_readvanceable_facility()
        is False -- not silently scored, not swallowed to -inf."""
        cfg = ic.load_and_map(no_heloc_input_json)
        results = optimize.run_optimization(cfg, no_heloc_input_json)
        assert results, "expected at least the non-readvanceable strategies"
        assert not any(r.get('readvanceable_mortgage') for r in results)
        # 'no_readvance' (the non-readvanceable baseline strategy) is
        # expected and fine; only the *prioritized* readvance strategy name
        # must never appear.
        assert not any(
            r.get('strategy', '').lower().startswith('readvance')
            for r in results
        )
        # And no candidate was silently swallowed to a hidden -inf score.
        assert all(r.get('net_benefit', 0) > float('-inf') for r in results)

    def test_header_reports_unavailable_not_a_fabricated_rate(
        self, cache_dir, no_heloc_input_json, monkeypatch, capsys,
    ):
        """The INPUTS header must not print a HELOC rate, an after-tax SM
        cost, or a net spread when there is no HELOC -- and must say plainly
        that Smith Manoeuvre is unavailable."""
        monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', no_heloc_input_json])
        optimize.main()

        out = capsys.readouterr().out
        assert 'Smith Manoeuvre unavailable' in out
        assert 'no readvanceable line on this contract' in out
        assert 'HELOC:' not in out
        assert 'After-tax (SM)' not in out
        assert 'Net spread' not in out

    def test_header_still_reports_the_spread_with_a_real_heloc(
        self, cache_dir, monkeypatch, capsys, tmp_path,
    ):
        """Sanity check: the fix does not silence a real HELOC household's
        header, only a nonexistent one's."""
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        path = tmp_path / "input.json"
        path.write_text(json.dumps(doc))
        monkeypatch.setattr(sys, 'argv', ['optimize.py', '--input', str(path)])
        optimize.main()

        out = capsys.readouterr().out
        assert 'HELOC:' in out
        assert 'Net spread' in out
        assert 'Smith Manoeuvre unavailable' not in out


# ---------------------------------------------------------------------------
# #713 x #663: a household can now AUTHOR its own strategies
# (decisions.contribution_strategy[]), and an authored strategy can set
# `use_smith: true`. That request must obey exactly the same gate as the
# engine's built-in readvance_priority -- otherwise wiring #713 would have
# re-opened #663 through the back door: the optimizer would rank a Smith
# Manoeuvre for a household whose mortgage is not readvanceable.
#
# The caller-side filter (has_readvanceable_facility) cannot catch this: it
# asks whether a revolving line EXISTS, not whether it is READVANCEABLE, so a
# split-charge structure (#687) passes it while still being unable to run the
# mechanism.
# ---------------------------------------------------------------------------

from countries.canada.strategies import discover_strategies
from strategy import FamilyState


def _state():
    return FamilyState(
        annual_savings=40000, primary_marginal_rate=0.45, spouse_marginal_rate=0.35,
        bracket_gap=0.10, primary_rrsp_room=30000, spouse_rrsp_room=20000,
        primary_tfsa_room=20000, spouse_tfsa_room=20000,
    )


_AUTHORED_SM = {
    'my_smith': {
        'name': 'My Smith Manoeuvre', 'rrsp_pct': 0.3, 'spousal_rrsp_pct': 0.1,
        'tfsa_pct': 0.2, 'fhsa_pct': 0.0, 'resp_pct': 0.1, 'non_reg_pct': 0.3,
        'prioritize_readvanceable': True, 'deduct_later': False,
    },
}


def test_authored_smith_strategy_is_dropped_when_the_mortgage_is_not_readvanceable():
    """#663's invariant, for a USER-AUTHORED strategy: never rank a strategy
    the household structurally cannot execute."""
    discovered = discover_strategies(
        _state(), {'heloc_readvance': False},
        investment_return=0.07, heloc_rate=0.05,
        custom_strategies=_AUTHORED_SM,
    )
    assert not any(s.prioritize_readvanceable for s in discovered.values()), (
        "an authored use_smith strategy was ranked against a mortgage that is "
        "not readvanceable -- the Smith Manoeuvre is structurally impossible "
        "here (#663), and custom_strategies must not bypass the gate (#713)"
    )


def test_authored_smith_strategy_survives_when_the_mortgage_IS_readvanceable():
    """DP#17, the other side of the threshold -- and the point of #713: when
    the household CAN execute it, their authored strategy must actually run."""
    discovered = discover_strategies(
        _state(), {'heloc_readvance': True},
        investment_return=0.07, heloc_rate=0.05,
        custom_strategies=_AUTHORED_SM,
    )
    assert 'my_smith' in discovered
    assert discovered['my_smith'].prioritize_readvanceable


def test_authored_smith_strategy_is_dropped_when_the_spread_is_unprofitable():
    """The gate is all three conditions, not just the readvanceable flag: a
    readvanceable line whose after-tax cost exceeds the after-tax return cannot
    profitably run the mechanism either."""
    discovered = discover_strategies(
        _state(), {'heloc_readvance': True},
        investment_return=0.01, heloc_rate=0.15,   # borrowing costs far more than it earns
        custom_strategies=_AUTHORED_SM,
    )
    assert not any(s.prioritize_readvanceable for s in discovered.values())


def test_a_non_smith_authored_strategy_is_never_collateral_damage():
    """The gate drops only the strategies that ASK for the mechanism."""
    authored = dict(_AUTHORED_SM)
    authored['my_balanced'] = {
        'name': 'My Balanced', 'rrsp_pct': 0.5, 'spousal_rrsp_pct': 0.1,
        'tfsa_pct': 0.2, 'fhsa_pct': 0.0, 'resp_pct': 0.1, 'non_reg_pct': 0.1,
        'prioritize_readvanceable': False, 'deduct_later': False,
    }
    discovered = discover_strategies(
        _state(), {'heloc_readvance': False},
        investment_return=0.07, heloc_rate=0.05,
        custom_strategies=authored,
    )
    assert 'my_balanced' in discovered
    assert 'my_smith' not in discovered
