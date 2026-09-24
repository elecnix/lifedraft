#!/usr/bin/env python3
"""Tests for SimulationConfig round-trip: to_dict, to_json, overlay_diff (DP#24).

Per DP#24: config round-trips enable save–modify–re-run workflows.
to_dict() is the inverse of from_dict(); overlay_diff shows what changed.
"""

import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast
import unittest
from dataclasses import fields as dataclass_fields, replace
from pathlib import Path
from typing import Set

import _example_doc
import config_serde
import input_contract as ic
from simulation import SimulationConfig


def _make_config():
    return SimulationConfig(
        projection_years=5, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
        mortgage_balance=100000, mortgage_rate=0.05,
        house_value=400000, ltv_max=0.80,
        deduct_later_bracket_target=117045,
    )


class TestConfigRoundTrip(unittest.TestCase):
    """DP#24: to_dict/from_dict must round-trip without loss."""

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict() → from_dict() produces equivalent config."""
        cfg = _make_config()
        d = cfg.to_dict()
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg.projection_years, cfg2.projection_years)
        self.assertEqual(cfg.investment_return, cfg2.investment_return)
        self.assertEqual(cfg.mortgage_balance, cfg2.mortgage_balance)
        self.assertEqual(cfg.house_value, cfg2.house_value)
        self.assertEqual(cfg.ltv_max, cfg2.ltv_max)
        self.assertEqual(cfg.deduct_later_bracket_target, cfg2.deduct_later_bracket_target)

    def test_roundtrip_preserves_tax_retirement_fields(self):
        """from_dict(to_dict()) preserves capital_gains_inclusion/tfsa_growth (#247).

        retirement_years dropped from this pin in epic #603 Track C Phase 2
        (DP#9): the field itself was deleted -- parsed onto
        SimulationConfig.retirement_years, only round-tripped via to_dict(),
        never read for a decision (#593's DEAD_ALLOWLIST).
        """
        cfg = replace(
            _make_config(),
            capital_gains_inclusion=0.66, tfsa_growth=0.05,
        )
        cfg2 = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(
            (cfg2.capital_gains_inclusion, cfg2.tfsa_growth),
            (cfg.capital_gains_inclusion, cfg.tfsa_growth),
        )

    def test_to_dict_contains_all_sections(self):
        """to_dict() contains all main sections."""
        cfg = _make_config()
        d = cfg.to_dict()
        self.assertIn('assumptions', d)
        self.assertIn('property', d)
        self.assertIn('family', d)
        self.assertIn('accounts', d)
        self.assertIn('savings', d)

    def test_to_dict_includes_deduct_later_bracket_target(self):
        """to_dict() includes the deduct_later_bracket_target field (DP#45)."""
        cfg = _make_config()
        d = cfg.to_dict()
        self.assertIn('deduct_later_bracket_target', d['accounts'])
        # DP#13: _make_config sets it to 117045 explicitly
        self.assertEqual(d['accounts']['deduct_later_bracket_target'], 117045)

    def test_from_dict_default_deduct_later(self):
        """from_dict() uses default deduct_later_bracket_target if missing (DP#13: 0 = auto-detect)."""
        cfg = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 3, 'investment_return': 0.07},
            'savings': {'rate': 0.0},
            'property': {'house_value': 400000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000},
            ]},
            'accounts': {},
        })
        # DP#13: default is 0 (auto-detect from tax brackets), not 117045
        self.assertEqual(cfg.deduct_later_bracket_target, 0)
        # But explicitly setting it works
        cfg_explicit = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 3, 'investment_return': 0.07,
                            'deduct_later_bracket_target': 117045},
            'savings': {'rate': 0.0},
            'property': {'house_value': 400000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000},
            ]},
            'accounts': {},
        })
        self.assertEqual(cfg_explicit.deduct_later_bracket_target, 117045)


class TestToJson(unittest.TestCase):
    """DP#24: to_json() produces valid JSON and optionally writes to file."""

    def test_to_json_returns_valid_json(self):
        """to_json() returns a parseable JSON string."""
        cfg = _make_config()
        j = cfg.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed['assumptions']['projection_years'], 5)

    def test_to_json_writes_to_file(self):
        """to_json(path=...) writes JSON to a file."""
        cfg = _make_config()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            path = f.name
        try:
            cfg.to_json(path=path)
            with open(path) as f:
                parsed = json.load(f)
            self.assertEqual(parsed['assumptions']['projection_years'], 5)
        finally:
            os.unlink(path)

    def test_to_json_roundtrip_with_from_dict(self):
        """to_json() → json.loads → from_dict() round-trips."""
        cfg = _make_config()
        j = cfg.to_json()
        parsed = json.loads(j)
        cfg2 = SimulationConfig.from_dict(parsed)
        self.assertEqual(cfg.projection_years, cfg2.projection_years)
        self.assertEqual(cfg.deduct_later_bracket_target, cfg2.deduct_later_bracket_target)


class TestOverlayDiff(unittest.TestCase):
    """DP#18, DP#24: overlay_diff shows what changed between configs."""

    def test_same_config_no_diff(self):
        """Identical configs produce empty overlays."""
        cfg = _make_config()
        diff = SimulationConfig.overlay_diff(cfg, cfg)
        self.assertEqual(diff['n_changes'], 0)
        self.assertEqual(len(diff['overlays']), 0)

    def test_ltv_overlay_shows_in_diff(self):
        """LTV overlay changes appear in the diff (DP#18)."""
        cfg = _make_config()
        cfg2 = replace(cfg, ltv_max=0.50)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertGreater(diff['n_changes'], 0)
        self.assertIn('property.ltv_max', diff['overlays'])

    def test_mortgage_overlay_shows_in_diff(self):
        """Mortgage balance overlay appears in the diff."""
        cfg = _make_config()
        cfg2 = replace(cfg, mortgage_balance=200000)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertIn('property.mortgage_balance', diff['overlays'])
        self.assertEqual(diff['overlays']['property.mortgage_balance']['from'], 100000)
        self.assertEqual(diff['overlays']['property.mortgage_balance']['to'], 200000)

    def test_deduct_later_overlay_shows_in_diff(self):
        """deduct_later_bracket_target overlay appears in the diff."""
        cfg = _make_config()
        cfg2 = replace(cfg, deduct_later_bracket_target=58523)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertIn('accounts.deduct_later_bracket_target', diff['overlays'])
        self.assertEqual(diff['overlays']['accounts.deduct_later_bracket_target']['from'], 117045)
        self.assertEqual(diff['overlays']['accounts.deduct_later_bracket_target']['to'], 58523)

    def test_overlay_diff_auditable(self):
        """Overlay diff is human-readable — can be inspected to see what changed."""
        cfg = _make_config()
        cfg2 = replace(cfg, ltv_max=0.65, mortgage_balance=250000)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        # Each overlay shows 'from' and 'to' values
        for key, change in diff['overlays'].items():
            self.assertIn('from', change)
            self.assertIn('to', change)


class TestSerdeParity(unittest.TestCase):
    """Issue #235: every SimulationConfig field is BOTH read by
    ``config_fields_from_dict()`` and re-emitted by ``config_to_dict()``.

    The two halves are hand-listed and each field has its own absence idiom;
    before this guard, a field with a read entry but no write entry (or vice
    versa) failed silently -- #729's ``lira`` and #730's ``cash_out`` were
    exactly that defect, and the old round-trip test named only ~6 fields.

    The read half is measured by EXECUTING the real ``config_fields_from_dict``
    against the canonical example document (its return keys are the
    constructor kwargs). The write half is measured STATICALLY -- every
    ``config.<name>`` access in an emission position inside ``config_to_dict``
    (a dict value, never a gate), so a field whose write lives inside a
    conditional block is still seen while a name that only guards an emission
    is not mistaken for one. Both lists are then compared to the dataclass
    declaration itself; a one-sided field fails with its exact name, not a
    count.
    """

    # Fields whose from_dict() value is DERIVED from other round-tripped
    # keys, not read as declared data -- a derived value has nothing to
    # preserve, so the write half correctly does not re-emit it (re-emitting
    # would be a DP#18 dead write: nothing reads it back). Each entry is
    # mechanically re-verified in test_derived_exceptions_are_not_readable,
    # so a stale entry fails the build instead of rotting.
    DERIVED_NO_WRITE: Set[str] = {
        # #663: from_dict computes has_heloc as
        # 'margin_available' in cfg['property'] (has_readvanceable_facility)
        # -- the PRESENCE of the key, never a value read off it. to_dict does
        # not write has_heloc as a field; it GATES the emission of
        # property.margin_available on it (#99: emitted iff has_heloc), so
        # the key's presence carries the derived value across the round
        # trip. A separate has_heloc key would be a DP#18 dead write: nothing
        # reads it back. Behaviour locked by
        # tests/test_issue_99_has_heloc_roundtrip.py.
        'has_heloc',
    }

    @classmethod
    def _read_half_fields(cls):
        """The fields from_dict() constructs: config_fields_from_dict's keys.

        Run against the canonical two-generation example document mapped to
        the internal shape -- the maximally populated config the issue's
        spec calls for -- so the guard exercises the real halves on a real
        document, not a hand-built stub.
        """
        cfg = ic.to_internal_config(_example_doc.minimal_example())
        return set(config_serde.config_fields_from_dict(cfg).keys())

    @classmethod
    def _write_half_fields(cls):
        """The fields to_dict() re-emits: every ``config.<name>`` access in
        an *emission position* -- the value of one of the dicts being built --
        in the source of ``config_to_dict``. Static (AST over config_serde.py),
        so a field whose emission is gated behind ``if config.X is not None``
        or a ``**({...} if ... else {})`` spread is still counted -- exactly
        the conditional shape every absence-idiom field uses here.

        A *reference* is not an *emission*: ``config.X`` appearing only in a
        gate -- ``**({} if config.X is None else {})`` -- writes nothing, so
        it must not count. Counting it would let a field whose write entry
        was deleted (leaving only the guard behind) pass as written and be
        silently lost on round-trip (#235).
        """
        tree = ast.parse(Path(config_serde.__file__).read_text())

        def _emitted(out: Set[str], n: ast.AST) -> None:
            """Collect ``config.<attr>`` from emission positions under *n*:
            dict values (a ``**{...}`` spread is a None-keyed entry whose
            value is walked the same), never dict keys, and never the
            ``test`` of an ``ast.IfExp`` / ``ast.If`` met on the way -- a
            gate is not an emission.
            """
            if isinstance(n, (ast.IfExp, ast.If)):
                for branch in (n.body, n.orelse):  # type: ignore[attr-defined]
                    _emitted(out, branch)
            elif isinstance(n, ast.Dict):
                for value in n.values:
                    _emitted(out, value)
            elif isinstance(n, ast.Attribute):
                if isinstance(n.value, ast.Name) and n.value.id == 'config':
                    out.add(n.attr)
                _emitted(out, n.value)
            else:
                for child in ast.iter_child_nodes(n):
                    _emitted(out, child)

        fields: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'config_to_dict':
                _emitted(fields, node)
        return fields

    @classmethod
    def _declared_fields(cls):
        return {f.name for f in dataclass_fields(SimulationConfig)}

    def test_every_declared_field_is_read(self):
        """The read half must construct every dataclass field."""
        declared = self._declared_fields()
        read = self._read_half_fields()
        missing = declared - read
        self.assertEqual(
            missing, set(),
            f"fields read by from_dict but never declared on SimulationConfig, "
            f"or declared but never constructed by the read half: {sorted(missing)}",
        )
        self.assertEqual(
            read, declared,
            "read half and dataclass declaration disagree "
            f"(read-only: {sorted(read - declared)}; "
            f"declared-only: {sorted(declared - read)})",
        )

    def test_every_field_is_written_back(self):
        """The write half must re-emit every field the read half ingests,
        except the mechanically-verified derived fields -- a field with a
        read entry but no write entry is a silent data-loss defect (#729/
        #730), and the test names the exact one-sided fields when it fails.
        """
        declared = self._declared_fields()
        written = self._write_half_fields()
        missing = (declared - self.DERIVED_NO_WRITE) - written
        self.assertEqual(
            missing, set(),
            "one-sided mirrors: read by from_dict but never re-emitted by "
            f"to_dict: {sorted(missing)}. Each needs a write entry in "
            "config_to_dict (or a cited, mechanically-verified entry in "
            "DERIVED_NO_WRITE if it is derived, not declared data).",
        )

    def test_write_half_touches_no_undeclared_field(self):
        """The write half may only touch real fields -- a typo'd
        ``config.horizon__age`` would crash production to_dict anyway, but
        this pins it at the guard instead of at runtime."""
        declared = self._declared_fields()
        written = self._write_half_fields()
        self.assertEqual(
            sorted(written - declared), [],
            "config_to_dict reads attributes that are not SimulationConfig "
            "fields -- a typo or a dead write:",
        )

    def test_derived_exceptions_are_not_readable(self):
        """DERIVED_NO_WRITE entries must really be derived: their read half
        computes them by calling a helper, never by reading a config dict key
        of the same name. If a future from_dict starts ingesting a declared
        ``has_heloc`` key, this entry stops being an exception and must be
        re-triaged -- the guard refuses to let it rot.
        """
        tree = ast.parse(Path(config_serde.__file__).read_text())
        return_stmt = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == 'config_fields_from_dict'):
                for n in ast.walk(node):
                    if isinstance(n, ast.Return) and n.value is not None:
                        return_stmt = n.value
        self.assertIsNotNone(return_stmt, 'could not locate read half return')

        kwargs = {}
        if isinstance(return_stmt, ast.Call):
            for kw in return_stmt.keywords:
                kwargs[kw.arg] = kw.value
        else:  # dict(...) literal with keyword form
            self.fail('read half return is not a dict() call -- update guard')

        for name in sorted(self.DERIVED_NO_WRITE):
            self.assertIn(name, kwargs, f'{name} missing from read half')
            self.assertNotIsInstance(
                kwargs[name], ast.Attribute,
                f'{name} is read via a .get(...) dict access -- it is declared '
                f'data now, not derived; remove it from DERIVED_NO_WRITE and '
                f'give it a write entry in config_to_dict',
            )
            self.assertTrue(
                isinstance(kwargs[name], ast.Call),
                f'{name} is computed by an expression, not a dict access -- '
                f're-verify the derivation and update this guard if legitimate',
            )


if __name__ == '__main__':
    unittest.main()