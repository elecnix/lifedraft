#!/usr/bin/env python3
"""Tests pinning the contract of the ONE run-recorded-summary reader behind
model_fidelity's runtime caveats (the #685/#707 bridge).

Three caveats read a findings summary the optimize caller recorded onto the
cfg's ``assumptions`` block -- decumulation shortfall (#707), runway (#758),
and RRSP contribution refusals (#170). Before this dedup each carried its own
verbatim copy of the same reader body (clone-detection flagged the third copy
at 97%); now they delegate to one ``_run_recorded_summary`` helper, and these
tests pin the contract every consumer depends on:

  * a cfg with no ``assumptions`` block, a non-dict cfg, or an absent key
    yields that reader's all-clear defaults;
  * the all-clear dict is a FRESH copy per call (a caller mutating it cannot
    corrupt another reader or a later call -- DP#3);
  * a recorded summary is returned AS RECORDED, never merged with or
    overwritten by the defaults.

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_fidelity import (
    _run_recorded_summary,
    decumulation_shortfall_summary,
    rrsp_refusal_summary,
    runway_summary,
)

RECORDINGS = {
    'decumulation_shortfall': {
        'engaged': True, 'exhausted': True, 'first_shortfall_year': 2044,
        'first_shortfall_gap': 12_500.0, 'shortfall_years': 3,
        'total_unmet': 30_000.0,
    },
    'runway': {
        'engaged': True, 'runway_months': 41,
        'relies_on_credit_facility': False, 'drew_registered': True,
        'scenario_label': 'refi-80',
    },
    'rrsp_contribution_refused': {
        'engaged': True, 'first_refused_year': 2027,
        'refused_own_total': 5_000.0, 'refused_spousal_total': 10_000.0,
    },
}

READERS = {
    'decumulation_shortfall': decumulation_shortfall_summary,
    'runway': runway_summary,
    'rrsp_contribution_refused': rrsp_refusal_summary,
}


class TestSharedReaderContract:
    def test_each_reader_reads_its_own_key(self):
        """Each recorded summary reaches exactly its own reader -- the key is
        the only thing that differs between the three delegates."""
        for key, reader in READERS.items():
            cfg = {'assumptions': {key: dict(RECORDINGS[key])}}
            assert reader(cfg) == RECORDINGS[key], key

    def test_absent_everywhere_yields_all_clear(self):
        """No assumptions block / non-dict cfg / absent key: each reader gets
        its documented all-clear defaults (every reader's own tests already
        assert the specific fields; here we pin engaged=False at minimum)."""
        for key, reader in READERS.items():
            for cfg in ({}, None, {'assumptions': {}}):
                s = reader(cfg)
                assert isinstance(s, dict), (key, cfg)
                assert s.get('engaged') is False, (key, cfg)

    def test_all_clear_is_a_fresh_copy_per_call(self):
        """DP#3: mutating one call's all-clear dict must not leak into the
        next call (the shared helper must not hand out shared state)."""
        first = decumulation_shortfall_summary({})
        first['exhausted'] = True  # a hostile mutation by a caller
        second = decumulation_shortfall_summary({})
        assert second['exhausted'] is False

    def test_recorded_wins_over_defaults(self):
        """A recorded summary is returned as recorded -- defaults are never
        merged in or partially overwritten."""
        cfg = {'assumptions': {'runway': dict(RECORDINGS['runway'])}}
        s = runway_summary(cfg)
        assert s == RECORDINGS['runway']

    def test_helper_itself_honours_the_same_contract(self):
        """The shared reader is directly exercised too: fresh-copy defaults,
        recorded passthrough."""
        defaults = {'engaged': False}
        a = _run_recorded_summary({}, 'some_key', defaults)
        b = _run_recorded_summary({}, 'some_key', defaults)
        assert a == defaults and b == defaults and a is not b
        recorded = {'engaged': True, 'n': 1}
        assert _run_recorded_summary(
            {'assumptions': {'some_key': recorded}}, 'some_key',
            defaults) == recorded