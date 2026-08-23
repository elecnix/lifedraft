#!/usr/bin/env python3
"""Tests for issue #1000: a declared fhsa_room_accumulated under the DEFAULT
strategy must not stay silent.

The bug: ``SimState.initial`` builds the FHSA store straight from a declared
``fhsa_room_accumulated`` (has_fhsa True, adult_fhsa_total_room 8000), but
``StrategyEngine.allocate`` gates the FHSA sweep on ``strategy.fhsa_pct > 0``
and every default/built-in strategy carries ``fhsa_pct = 0.0`` -- so the
declared room moved nothing and terminal total_assets was byte-identical to
declaring zero. The "parsed, mapped, then never passed" silent-zero class.

Chosen semantics (issue outcome (c), warn): the default strategy's fhsa_pct is
NOT silently lifted off zero -- that would be an opinion, not a declaration
(DP#13), and would move the golden household itself, which declares 8 000 of
FHSA room and runs adapter.get_default_strategy(). Instead, constructing a
FamilySimulation whose household declares FHSA room + lifetime headroom while
the active strategy has fhsa_pct <= 0 logs a warning naming both facts (#654
precedent: logger.warning, not warnings.warn -- this repo's pytest config
treats warnings as errors). An explicit fhsa_pct > 0 keeps routing money and
stays silent; a household with no room stays silent too.

Every test here drives FamilySimulation.run() end to end -- no hand-built
engine state (DP#11). Fabricated round numbers, role-based names (DP#4/DP#15).
"""
import logging
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY

_WARN_FRAGMENT = "fhsa_pct unset/0"


def _make_config(fhsa_room=None):
    """Fabricated round-number config (DP#4/DP#15).

    fhsa_room=None omits the key entirely (the control); a number declares
    fhsa_room_accumulated on the primary, exactly the issue's perturbation.
    """
    primary = {'role': 'primary', 'gross_income': 130_000,
               'rrsp_room_accumulated': 100_000,
               'tfsa_room_accumulated': 30_000,
               'birth_year': 1985}
    if fhsa_room is not None:
        primary['fhsa_room_accumulated'] = fhsa_room
    return SimulationConfig(
        projection_years=3,
        investment_return=0.06,
        salary_growth=0.0,
        savings_rate=0.20,
        house_value=600_000,
        mortgage_balance=200_000,
        mortgage_rate=0.02,
        ltv_max=0.80,
        amortization_years=20,
        family_members=[
            primary,
            {'role': 'spouse', 'gross_income': 70_000,
             'rrsp_room_accumulated': 50_000,
             'tfsa_room_accumulated': 30_000,
             'birth_year': 1987},
        ],
        children=[],
    )


def _sim(config, **kwargs):
    return FamilySimulation(config, adapter=CanadaAdapter(config), **kwargs)


def _fhsa_contrib_total(results):
    return sum(r.contributions.get('fhsa', 0.0) for r in results)


class TestDeclaredRoomDefaultStrategyWarns:
    def test_declared_room_default_strategy_warns(self, caplog):
        """A household that declares FHSA room under the default strategy
        (fhsa_pct=0) gets a loud warning that the room moves nothing."""
        with caplog.at_level(logging.WARNING, logger='simulation'):
            sim = _sim(_make_config(fhsa_room=8_000))
        matches = [r for r in caplog.records if _WARN_FRAGMENT in r.message]
        assert matches, (
            "constructing a simulation with declared FHSA room and the "
            "default strategy (fhsa_pct=0) emitted no inert-room warning "
            "(issue #1000)"
        )
        assert len(matches) == 1, "warning must fire once at construction"

    def test_declared_room_default_strategy_still_contributes_zero(self):
        """Warn-only semantics: the run itself is unchanged -- the default
        strategy still routes $0 to the FHSA (no opinionated auto-enable,
        DP#13; the golden household depends on this)."""
        results = _sim(_make_config(fhsa_room=8_000)).run()
        assert _fhsa_contrib_total(results) == 0.0

    def test_explicit_fhsa_pct_routes_money_and_stays_silent(self, caplog):
        """The explicit path is untouched: fhsa_pct > 0 routes nonzero FHSA
        contributions through FamilySimulation.run() and emits NO warning."""
        strat = replace(STRATEGY_READVANCE_PRIORITY,
                        fhsa_pct=0.10,
                        non_reg_pct=STRATEGY_READVANCE_PRIORITY.non_reg_pct - 0.10)
        with caplog.at_level(logging.WARNING, logger='simulation'):
            results = _sim(_make_config(fhsa_room=8_000), strategy=strat).run()
        assert _fhsa_contrib_total(results) > 0.0, (
            "an explicit fhsa_pct > 0 must still route FHSA contributions "
            "(regression guard on the pre-existing working path)"
        )
        assert not [r for r in caplog.records if _WARN_FRAGMENT in r.message], (
            "a strategy that actually sweeps to the FHSA must not be warned "
            "about"
        )

    def test_no_room_control_emits_no_warning(self, caplog):
        """Control: a household that declares no FHSA room is never warned --
        the warning keys on DECLARED room, not on the default strategy."""
        with caplog.at_level(logging.WARNING, logger='simulation'):
            _sim(_make_config())  # no fhsa_room_accumulated anywhere
        assert not [r for r in caplog.records if _WARN_FRAGMENT in r.message]

    def test_zero_is_a_value_not_absence_but_room_of_zero_is_not_room(self, caplog):
        """An explicit fhsa_room_accumulated: 0 builds no room (DP#32: the 0
        is honoured as a value) and therefore warns about nothing."""
        with caplog.at_level(logging.WARNING, logger='simulation'):
            _sim(_make_config(fhsa_room=0))
        assert not [r for r in caplog.records if _WARN_FRAGMENT in r.message]
