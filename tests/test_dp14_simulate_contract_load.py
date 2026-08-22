#!/usr/bin/env python3
"""DP#14 lock (#987): simulate.py must not expose a raw ``json.load`` path.

``simulate.load_inputs()`` did a bare ``json.load`` and was re-exported as
public API from the package ``__init__`` -- a contract-bypassing ingestion
boundary (no schema validation, no jurisdiction-overlay mapping, no loud
refusal on an invalid document). The identical shim was already excised
from ``optimize.py`` and ``sensitivity.py`` with tests enforcing its
absence; this file closes the last gap.

The canonical boundary is ``input_contract.load_and_map(path)`` (or
``SimulationConfig.from_json``, which delegates to it): validate, then map
to the internal shape. simulate.py's own ``main()`` already used it --
these tests pin that, and pin that an invalid document refuses loudly
(DP#32: a refusal is a feature) instead of silently loading.
"""
import inspect
import json
import os
import tempfile

import pytest

import input_contract as ic
import simulate
import simulation_config
from simulation_config import SimulationConfig


def _example_contract_json(tmp_path) -> str:
    """The shipped example contract written to disk (fabricated round
    numbers only -- DP#15)."""
    from test_input_contract import _load_example
    path = tmp_path / "input.json"
    path.write_text(json.dumps(_load_example()))
    return str(path)


class TestLoadInputsRemoved:
    """Lock: the contract-bypassing shim is gone (DP#9/DP#14)."""

    def test_load_inputs_removed(self):
        assert not hasattr(simulate, "load_inputs"), (
            "simulate.load_inputs() bypasses the input contract "
            "(raw json.load, no validation, no overlay mapping); use "
            "input_contract.load_and_map / SimulationConfig.from_json"
        )

    def test_not_reexported_from_package(self):
        from root_package_loader import load_root_package
        root = load_root_package()
        assert "load_inputs" not in getattr(root, "__all__", [])
        assert not hasattr(root, "load_inputs")

    def test_main_block_uses_load_and_map(self):
        """simulate.py's CLI must ingest through input_contract.load_and_map
        and never mention load_inputs."""
        source = inspect.getsource(simulate)
        assert "input_contract.load_and_map" in source
        assert "load_inputs" not in source


class TestCanonicalBoundary:
    """What callers should use instead -- and that it refuses loudly."""

    def test_from_json_equals_load_and_map(self):
        """SimulationConfig.from_json delegates to load_and_map: both entry
        points must produce an identical config for a valid document."""
        from test_input_contract import _load_example, _two_generation_subset
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "input.json")
            with open(path, "w") as f:
                json.dump(_two_generation_subset(_load_example()), f)
            via_from_json = SimulationConfig.from_json(path)
            via_load_and_map = SimulationConfig.from_dict(ic.load_and_map(path))
        assert via_from_json == via_load_and_map

    def test_invalid_document_refuses_loudly(self, tmp_path):
        """An additionalProperties violation must raise the contract's
        validation error at load time -- never silently load."""
        from test_input_contract import _load_example
        doc = _load_example()
        doc["definitely_not_in_the_schema"] = True  # unknown top-level key
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(doc))
        with pytest.raises(ic.ContractValidationError):
            ic.load_and_map(str(path))

    def test_missing_required_key_refuses_loudly(self, tmp_path):
        from test_input_contract import _load_example
        doc = _load_example()
        del doc["as_of"]
        path = tmp_path / "incomplete.json"
        path.write_text(json.dumps(doc))
        with pytest.raises(ic.ContractValidationError):
            simulation_config.SimulationConfig.from_json(str(path))
