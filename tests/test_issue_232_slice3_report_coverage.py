#!/usr/bin/env python3
"""Issue #232 slice 3: cover the report-rendering branches that moved from
optimize.py to output_plugins.py as uncovered lines.

The per-file coverage gate (tools/coverage_gate.py) counts uncovered LINES per
file. Relocating a function moves its uncovered lines with it: the six branches
below were uncovered in optimize.py before the move, and would otherwise appear
as a +6 regression in output_plugins.py. The answer the gate asks for is a test,
not a regenerated baseline (AGENTS.md), so this file drives exactly those six
branches -- each one a real, reachable path a household can hit.

  * output_plugins.py:2426 -- _print_income_scenario_report's "one scenario, nothing
    to compare" early return.
  * output_plugins.py:2824 -- _print_structure_report_for_basis's "best STRUCTURE
    changes under job loss" warning.
  * output_plugins.py:3068/3070 -- _print_decumulation_shortfall_report's label
    fallback (a row carrying neither `strategy` nor `label`).
  * output_plugins.py:3093 -- _objective_name_for_results()' empty-results return.
  * output_plugins.py:3162 -- _print_solvency_report's "nothing left to sell" branch.

Also pins the slice-3 seam shape: optimize.py must not re-export the moved
printers under their old private names (DP#9 -- callers import output_plugins).

Fabricated round numbers and role-free labels only (DP#15).
"""

import optimize
import output_plugins


def _scenario_row(sid, label, structure_id, structure_label, net_benefit):
    """One structure-ranking row with NO solvency/runway summary -- the
    synthetic shape the pure-logic tests build, whose UNCHECKED markers this
    file also exercises."""
    return {
        'structure_id': structure_id,
        'structure_label': structure_label,
        'income_scenario_id': sid,
        'income_scenario_label': label,
        'strategy': 's',
        'net_benefit': net_benefit,
    }


# ── output_plugins.py:2426 ───────────────────────────────────────────────────

def test_income_scenario_report_single_scenario_is_silent(capsys):
    """One declared scenario is nothing to compare -- the section is skipped
    rather than printed with a meaningless 'no change' row."""
    output_plugins._print_income_scenario_report([
        {'income_scenario_id': 'base', 'income_scenario_label': 'Base',
         'strategy': 's', 'net_benefit': 100.0},
    ])
    assert capsys.readouterr().out == ''


# ── output_plugins.py:2824 ───────────────────────────────────────────────────

def test_structure_report_warns_when_the_best_structure_changes(capsys):
    """Structure A wins at base income; B wins under the shock. The report must
    say so, not print two tables and leave the reader to diff them."""
    output_plugins._print_structure_report_for_basis([
        _scenario_row('base', 'Base income', 'A', 'All mortgage', 100.0),
        _scenario_row('base', 'Base income', 'B', 'Readvanceable', 50.0),
        _scenario_row('shock', 'Job loss', 'A', 'All mortgage', 10.0),
        _scenario_row('shock', 'Job loss', 'B', 'Readvanceable', 90.0),
    ])
    out = capsys.readouterr().out
    assert "Best STRUCTURE changes under 'Job loss'" in out
    assert 'Readvanceable' in out


# ── output_plugins.py:3068/3070 ──────────────────────────────────────────────

def test_decumulation_report_falls_back_when_labels_are_absent(capsys):
    """A row carrying neither `strategy` nor `label` still renders its
    shortfall -- as '?', never a crash and never a dropped row (DP#32)."""
    summary = {'engaged': True, 'exhausted': True, 'first_shortfall_year': 27,
               'first_shortfall_gap': 205_784.0, 'shortfall_years': 19,
               'total_unmet': 3_000_000.0}
    output_plugins._print_decumulation_shortfall_report(
        [{'drawdown_shortfall': summary, 'exhausted': True}], {})
    out = capsys.readouterr().out
    assert 'year 27' in out
    assert '?' in out


# ── output_plugins.py:3093 ───────────────────────────────────────────────────

def test_objective_name_for_results_is_none_for_no_results():
    assert output_plugins._objective_name_for_results([]) is None


# ── output_plugins.py:3162 ───────────────────────────────────────────────────

def test_solvency_report_names_an_empty_waterfall(capsys):
    """A shortfall year with no source drawn at all says "nothing left to
    sell" -- an empty list rendered as silence would read as "no problem"."""
    output_plugins._print_solvency_report([{
        'label': 'Job loss',
        'solvency': {
            'engaged': True,
            'runway_months_at_start': 3.0,
            'first_shortfall_year': 2,
            'forced_liquidation_gross_by_source': {},
        },
    }])
    assert '(nothing left to sell' in capsys.readouterr().out


# ── the seam shape (DP#9) ────────────────────────────────────────────────────

def test_optimize_does_not_re_export_the_moved_printers():
    """The printers live in output_plugins; optimize.py imports them inside
    main(), never as module-level aliases. A re-export shim under the old
    private names would defeat the move (DP#9)."""
    for name in (
        '_print_ltv_exploration', '_print_income_scenario_report',
        '_print_structure_report', '_print_structure_report_for_basis',
        '_print_property_funding_report', '_print_borrow_to_invest_report',
        '_print_decumulation_shortfall_report', '_print_solvency_report',
        '_print_runway_report', '_print_refinance_basis',
        'winners_by_income_scenario', 'winners_by_structure_scenario',
    ):
        assert not hasattr(optimize, name), (
            f'optimize.{name} was moved to output_plugins (#232 slice 3); '
            f'a module-level alias would be a re-export shim (DP#9)')
