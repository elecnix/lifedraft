"""Enforcement for issue #782: VOI must not report an OPTIMUM-NEUTRAL lever as
"$0, doesn't matter" when it still swings suboptimal strategies.

## The measured bug

``voi._score`` returns the argmax over strategies, so a leaf's VOI spread is the
movement of the BEST-achievable objective value. A fact that moves *suboptimal*
strategies a lot but leaves the argmax strategy unchanged therefore scores $0
and lands in ``INERT`` -- reported to the household as not mattering. But "the
optimal strategy is indifferent to X" is a genuinely different statement from
"X doesn't matter": on the estate-live fixture, toggling
``/estate/default_spousal_rollover`` under ``max_after_tax_estate`` leaves the
argmax (Non-registered-first) untouched yet moves RRSP-meltdown by ~$71k and
Bracket-filling by ~$53k. The fix surfaces that per-strategy sensitivity so an
optimum-neutral-but-material lever is disclosed, not buried at $0.

This file asserts two, non-overlapping claims:

  1. ``_strategy_sensitivity`` measures the WHOLE-ranking movement the argmax
     hides (a pure unit test -- no simulation).
  2. ``render_report`` DISCLOSES an optimum-neutral-but-strategy-sensitive
     leaf as such -- with its dollar figure -- instead of printing the flat
     "$0, doesn't matter" that conflates it with a genuinely inert leaf; and it
     still calls a leaf that moves NO strategy genuinely inert (DP#32).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voi

DEFAULT_ROLLOVER = "/estate/default_spousal_rollover"


# ═══════════════════════════════════════════════════════════════════════════
# 1. The pure measurement: whole-ranking movement, not just the argmax
# ═══════════════════════════════════════════════════════════════════════════

def test_strategy_sensitivity_sees_movement_the_argmax_hides():
    """The winner is flat across both samples ($100 either way), so the argmax
    spread is $0 -- yet two other strategies move. ``_strategy_sensitivity``
    reports the largest per-strategy spread and how many strategies moved."""
    on = {"winner": 100.0, "meltdown": 30.0, "bracket": 20.0, "tfsa": 10.0}
    off = {"winner": 100.0, "meltdown": 99.0, "bracket": 72.0, "tfsa": 10.0}

    assert voi._best(on) == voi._best(off)            # argmax is optimum-neutral
    spread, moved = voi._strategy_sensitivity([on, off])
    assert moved == 2                                  # meltdown + bracket, not tfsa
    assert spread == 69.0                              # the largest single-strategy swing (meltdown)


def test_strategy_sensitivity_is_zero_when_truly_nothing_moves():
    """Genuinely inert: every strategy identical across samples -> (0.0, 0),
    which is what lets the report say 'moves NO strategy' honestly (DP#32)."""
    same = {"a": 5.0, "b": 3.0}
    assert voi._strategy_sensitivity([same, dict(same)]) == (0.0, 0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. The report DISCLOSES the distinction the argmax hides (#782)
#
# A pure render test: the sweep files an optimum-neutral leaf in `inert`, and
# the fix's job is that `render_report` must not print that as a bare "$0 --
# doesn't matter" when the leaf still swings suboptimal strategies. Constructing
# the Finding directly (no simulation) tests exactly the reporting contract,
# deterministically -- what the household actually reads.
# ═══════════════════════════════════════════════════════════════════════════

def _inert_finding(pointer: str, strategy_spread: float, strategies_moved: int) -> voi.Finding:
    return voi.Finding(
        pointer=pointer, question="?", current_value=True, confidence="assumed",
        spec_source="schema", range_label="domain: {true, false}", resolved_by=None,
        spread=0.0, low_label=False, high_label=True, resolvability="document",
        moves_under=(), strategy_spread=strategy_spread, strategies_moved=strategies_moved,
    )


def _report_of(*inert: voi.Finding) -> voi.VOIReport:
    return voi.VOIReport(
        ranked=[], unread=[], inert=list(inert), unranked_pointers=[], dropped_pointers=[],
        structural_skipped=0, objective_name="max_after_tax_estate", baseline_score=0.0,
        dead_key_pass_ran=True, cross_objective_checked=False,
    )


def test_report_discloses_optimum_neutral_but_strategy_sensitive():
    """The heart of #782: an optimum-neutral lever that still swings suboptimal
    strategies by tens of thousands must be disclosed AS SUCH -- with its dollar
    figure -- not printed as a bare $0 that reads 'doesn't matter'."""
    finding = _inert_finding(DEFAULT_ROLLOVER, strategy_spread=71_457.0, strategies_moved=2)
    text = voi.render_report(_report_of(finding))

    assert DEFAULT_ROLLOVER in text
    assert "OPTIMUM-NEUTRAL BUT STRATEGY-SENSITIVE" in text
    assert "$71,457" in text
    assert "2 strategies" in text
    assert "doesn't matter" in text            # only ever in the negating phrase, never as a verdict


def test_report_says_genuinely_inert_when_no_strategy_moves():
    """The other side of the distinction (DP#32): a leaf that moves NO strategy
    must be reported as genuinely inert, NOT dressed up as strategy-sensitive."""
    finding = _inert_finding("/estate/tfsa_successor_holder", strategy_spread=0.0, strategies_moved=0)
    text = voi.render_report(_report_of(finding))

    assert "OPTIMUM-NEUTRAL BUT STRATEGY-SENSITIVE" not in text
    assert "moves NO strategy under this objective" in text
