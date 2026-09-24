"""Issue #231 slice 1 enforcement: ``YearInputs`` must stay frozen.

The reshape bundles the year step's per-call inputs (56 at the time; 55 since
issue #277 moved the prior year's GIS-countable income into ``SimState``) into
ONE object so a rule can no longer rebind or replace a field. That guarantee -- rules read
their inputs and cannot mutate them -- is the *reason* the reshape is an
improvement, and it is what DP#26's "pure function over explicit state"
depends on. Nothing else in the suite notices when it evaporates: dropping
``frozen=True`` leaves every existing test green while the guarantee silently
disappears (that control was run before this file was written).

So the guard asserts the *behaviour*, not the declaration: it attempts a real
field rebinding and requires ``dataclasses.FrozenInstanceError``. Reading
``YearInputs.__dataclass_params__.frozen`` would be a test of the decorator's
spelling, and would pass for any future construct that happens to carry the
flag while still allowing mutation.

Scope, stated honestly: this proves field *rebinding* is refused. It does not
prove deep immutability -- ``allocations`` and other collection fields are
still the caller's own mutable objects, and a rule can still mutate their
*contents*. That shallow boundary is intentional (the engine needs to read the
live dicts) and is not what this guard claims to cover.
"""
from __future__ import annotations

import dataclasses

import pytest

from simulation_config import SimulationConfig
from simulation_state import YearInputs, _build_year_inputs


def _year_inputs() -> YearInputs:
    return _build_year_inputs(
        allocations={"_primary_income": 0.0},
        config=SimulationConfig(),
        investment_return=0.0,
    )


def test_year_inputs_rejects_field_rebinding():
    """A rule that assigns to a YearInputs field must raise, not succeed."""
    inputs = _year_inputs()
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.investment_return = 0.07


def test_year_inputs_rejects_field_deletion():
    """``del`` is the other write a rule could attempt on an input."""
    inputs = _year_inputs()
    with pytest.raises(dataclasses.FrozenInstanceError):
        del inputs.investment_return


def test_year_inputs_survives_a_failed_rebinding_unchanged():
    """The refused write must leave the object's values untouched."""
    inputs = _year_inputs()
    allocations = inputs.allocations
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.investment_return = 0.07
    assert inputs.investment_return == 0.0
    assert inputs.allocations is allocations
