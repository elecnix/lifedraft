#!/usr/bin/env python3
"""Issue #99 (DP#24/DP#32): the "does a readvanceable HELOC exist" state must
survive ``from_dict -> to_dict -> from_dict`` losslessly.

``from_dict`` derives ``has_heloc`` from the PRESENCE of
``property.margin_available`` (``config_access.has_readvanceable_facility``,
canonical since #663). Before this fix ``config_to_dict`` wrote
``margin_available`` unconditionally, so a household with no HELOC loaded as
``has_heloc=False``, exported ``margin_available: 0``, and reloaded as
``has_heloc=True`` -- and every raw-dict consumer of the exported dict
(``--export-config``, sensitivity.py's ``to_dict() -> apply_overlay``) was
handed a fabricated zero-room facility.

The converse matters just as much: a DECLARED facility with zero undrawn room
is a different state from no facility (DP#32 -- zero is a value, not
absence), so it must keep re-emitting ``margin_available: 0``. A truthiness
gate would silently erase it; the zero-room tests below exist to catch that
over-fix.

Every fixture is produced by the real contract adapter
(``input_contract.to_internal_config``) on the shipped fabricated example
contract (DP#11, DP#15) -- no hand-written internal property dicts.
"""
import dataclasses
import json
import sys

import pytest

import input_contract as ic
import optimize
from config_access import has_readvanceable_facility
from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig
from test_input_contract import _load_example, _two_generation_subset
from test_issue_663_no_heloc import (  # noqa: F401  (pytest fixtures)
    _mapped_no_heloc_cfg,
    cache_dir,
    no_heloc_input_json,
)


def _all_keys(obj):
    """Every dict key at any nesting depth of ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


def _heloc_liability(doc: dict) -> dict:
    helocs = [l for l in doc["liabilities"] if l["kind"] == "heloc"]
    assert len(helocs) == 1, f"expected exactly one heloc liability, got {len(helocs)}"
    return helocs[0]


def _mapped_zero_room_heloc_cfg() -> dict:
    """The shipped example with its HELOC fully drawn (balance == limit): a
    DECLARED facility with zero undrawn room."""
    doc = _two_generation_subset(_load_example())
    heloc = _heloc_liability(doc)
    heloc["balance"]["amount"] = heloc["limit"]
    return ic.to_internal_config(doc)


def _mapped_heloc_with_room_cfg() -> dict:
    """The shipped example, unmodified: an undrawn HELOC with room."""
    return ic.to_internal_config(_two_generation_subset(_load_example()))


# ── The defect: no HELOC must stay no HELOC ─────────────────────────────

def test_no_heloc_roundtrip_stays_no_heloc():
    cfg = _mapped_no_heloc_cfg()
    assert 'margin_available' not in cfg['property']  # precondition (#663)

    c1 = SimulationConfig.from_dict(cfg)
    assert c1.has_heloc is False

    d = c1.to_dict()
    # The regression rests on these two: the exported dict must not declare
    # a facility (key presence IS the declaration) ...
    assert 'margin_available' not in d['property']
    assert has_readvanceable_facility(d) is False

    # ... and the reload must still say "no facility".
    c2 = SimulationConfig.from_dict(d)
    assert c2.has_heloc is False
    assert c2.margin_available == 0
    assert c2.is_readvanceable is False

    # Fixed point after one round trip (DP#24).
    assert c2.to_dict() == d


def test_no_heloc_export_writes_no_has_heloc_key():
    """The fix gates an existing key; it must not invent a ``has_heloc``
    key nothing reads back (DP#18 dead write)."""
    d = SimulationConfig.from_dict(_mapped_no_heloc_cfg()).to_dict()
    assert 'has_heloc' not in set(_all_keys(d))


# ── Guards against over-fixing: a declared HELOC stays declared ─────────

def test_zero_room_heloc_roundtrip_stays_heloc():
    cfg = _mapped_zero_room_heloc_cfg()
    # Precondition: the adapter maps a fully drawn HELOC to a PRESENT key
    # whose value is exactly 0 -- "facility, zero room", not "no facility".
    assert 'margin_available' in cfg['property']
    assert cfg['property']['margin_available'] == 0

    c1 = SimulationConfig.from_dict(cfg)
    assert c1.has_heloc is True
    assert c1.margin_available == 0

    d = c1.to_dict()
    assert 'margin_available' in d['property']
    assert d['property']['margin_available'] == 0
    assert has_readvanceable_facility(d) is True

    c2 = SimulationConfig.from_dict(d)
    assert c2.has_heloc is True
    assert c2.margin_available == 0
    assert c2.to_dict() == d


def test_declared_heloc_with_room_roundtrips():
    cfg = _mapped_heloc_with_room_cfg()
    assert cfg['property']['margin_available'] == 150_000  # precondition

    c1 = SimulationConfig.from_dict(cfg)
    d = c1.to_dict()
    assert d['property']['margin_available'] == 150_000
    assert has_readvanceable_facility(d) is True

    c2 = SimulationConfig.from_dict(d)
    assert c2.has_heloc is True
    assert c2.margin_available == 150_000
    assert c2.to_dict() == d


# ── Loud refusal: the gate must never silently drop a supplied value ────

def test_inconsistent_no_heloc_with_margin_refuses_loudly():
    """has_heloc=False with non-zero room is contradictory. from_dict can
    never produce it, but direct construction/replace can; the gate would
    otherwise drop the supplied margin without a word (DP#32)."""
    c = dataclasses.replace(
        SimulationConfig.from_dict(_mapped_no_heloc_cfg()), margin_available=50_000,
    )
    with pytest.raises(ValueError, match='has_heloc') as exc:
        c.to_dict()
    assert 'margin_available' in str(exc.value)


# ── Raw-dict consumers receive the fixed dict ───────────────────────────

def test_overlay_on_exported_no_heloc_dict_sees_no_facility():
    """sensitivity.py's shape: ``config.to_dict()`` handed to
    ``apply_overlay``. The overlay must not be given a facility the
    household does not have."""
    cfg = _mapped_no_heloc_cfg()
    exported = SimulationConfig.from_dict(cfg).to_dict()
    overlay = ScenarioOverlay(label="refinance", cash_out=50_000,
                              mortgage_rate=cfg['property']['mortgage_rate'])
    result = apply_overlay(exported, overlay)
    assert 'margin_available' not in result['property']
    assert has_readvanceable_facility(result) is False


def test_export_config_cli_omits_margin_for_no_heloc(
    cache_dir, no_heloc_input_json, tmp_path, monkeypatch,  # noqa: F811
):
    """End to end through the real CLI: ``optimize.py --input <mortgage-only
    contract> --export-config out.json`` must not export a HELOC."""
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, 'argv', [
        'optimize.py', '--input', no_heloc_input_json, '--export-config', str(out),
    ])
    optimize.main()

    exported = json.loads(out.read_text())
    assert 'margin_available' not in exported['property']
    assert has_readvanceable_facility(exported) is False
    assert SimulationConfig.from_dict(exported).has_heloc is False
