"""Time-horizon input parameter (DP#1: store dates, derive the span).

`assumptions.horizon_age` is the source of truth for "plan until the primary
turns N". The projection span (`projection_years`) is *derived* from it against
the primary's birth_year and start_year, rather than being hand-counted — a
derived count goes stale the moment start_year or birth_year moves (DP#1).

`projection_years` remains the engine's explicit span and the fallback when no
horizon_age is given (DP#13: defaults are fallbacks, not opinions).
"""
import pytest

from simulation_config import SimulationConfig


def _cfg(assumptions, birth_year=1980):
    """Fabricated round-number config, role-based names (DP#4, DP#15)."""
    members = ([{'role': 'primary', 'birth_year': birth_year, 'gross_income': 100_000}]
               if birth_year else
               [{'role': 'primary', 'gross_income': 100_000}])
    return {'family': {'members': members}, 'assumptions': assumptions}


def test_horizon_age_derives_projection_years():
    """Plan to 95 from age 45 (2025-1980) → 51 years: 2025..2075 inclusive."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2025, 'horizon_age': 95}))
    assert cfg.projection_years == 51


def test_last_simulated_year_lands_on_horizon_age():
    """Invariant: start_year + projection_years - 1 is the year primary is horizon_age."""
    start, birth, horizon = 2026, 1980, 90
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': start, 'horizon_age': horizon}, birth_year=birth))
    last_year = start + cfg.projection_years - 1
    assert last_year - birth == horizon


def test_horizon_age_overrides_explicit_projection_years():
    """The expressive form wins: horizon_age supersedes a hand-counted span."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2026, 'horizon_age': 90, 'projection_years': 10}))
    assert cfg.projection_years == 45  # 90 - (2026-1980) + 1


def test_projection_years_used_when_no_horizon_age():
    """Fallback (DP#13): without horizon_age the explicit span is honoured."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2026, 'projection_years': 12}))
    assert cfg.projection_years == 12


def test_falls_back_when_primary_has_no_birth_year():
    """Without a birth_year the horizon cannot be dated → explicit span stands."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2026, 'horizon_age': 95, 'projection_years': 7},
             birth_year=None))
    assert cfg.projection_years == 7


@pytest.mark.parametrize('horizon_age,expected', [
    (46, 1),   # horizon == current age → one (final) year, not zero
    (45, 1),   # horizon already passed → clamped to a single year, never negative
    (47, 2),   # one year beyond current age
])
def test_horizon_at_or_before_current_age_clamps_to_one_year(horizon_age, expected):
    """Threshold rule path (DP#17): the span is always >= 1 year."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2026, 'horizon_age': horizon_age}))  # primary is 46
    assert cfg.projection_years == expected


def test_horizon_age_round_trips(tmp_path):
    """DP#24: to_dict() re-emits horizon_age so the config can be saved and re-run."""
    cfg = SimulationConfig.from_dict(
        _cfg({'start_year': 2026, 'horizon_age': 95}))
    out = cfg.to_dict()
    assert out['assumptions']['horizon_age'] == 95
    # Re-loading the exported config reproduces the same span.
    assert SimulationConfig.from_dict(out).projection_years == cfg.projection_years
