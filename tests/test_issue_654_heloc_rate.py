#!/usr/bin/env python3
"""Tests for issue #654: the HELOC's own rate never reached the engine.

A ``kind=heloc`` liability declares its own ``rate`` -- a different credit
product from the mortgage it may sit alongside (a cheap legacy fixed
mortgage plus a prime-linked revolving HELOC is the ordinary shape this
tool exists for, not an edge case). Before this fix, that rate was parsed
by ``input_contract.py`` and then discarded: ``SimulationConfig`` had no
``heloc_rate`` field at all, and ``simulation.py``'s engine built its HELOC
interest path from ``property.mortgage_rate`` (via
``adapter.build_heloc_path(self.rate_path)``) instead -- silently pricing
Smith-Manoeuvre borrowing at the mortgage's rate, always in the direction
that flatters leverage (issue #595B's still-live alias-precedence bug).

Root assertion this file exists to pin down and keep pinned down (per
#654's enforcement ask): a document whose HELOC rate differs from its
mortgage rate MUST produce different HELOC interest than one where they
are equal. ``tests/test_schema_coverage.py``'s kind-aware liabilities[]
rewrite (also #654) prevents a future regression from hiding behind a
kind-blind citation the way this one did; this file is the belt to that
guard's suspenders -- a citation can be technically true and the number
can still be wrong, which is exactly what happened here.

DP#15: all data below is fabricated, round, role-based (DP#4).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import json
import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter

import input_contract as ic
import contract_schema


def _make_config(mortgage_rate, heloc_rate, margin_available=100_000):
    """Fabricated round-number config (DP#4/DP#15)."""
    return SimulationConfig(
        projection_years=3,
        investment_return=0.06,
        salary_growth=0.0,
        savings_rate=0.10,
        house_value=600_000,
        mortgage_balance=200_000,
        mortgage_rate=mortgage_rate,
        ltv_max=0.80,
        amortization_years=20,
        margin_available=margin_available,
        heloc_readvance=False,  # isolate the margin-draw interest path (#577), not SM readvance
        heloc_rate=heloc_rate,
        family_members=[
            {'role': 'primary', 'gross_income': 130_000,
             'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1985},
            {'role': 'spouse', 'gross_income': 70_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1987},
        ],
        children=[],
    )


def _sim(config, **kwargs):
    return FamilySimulation(config, adapter=CanadaAdapter(config), **kwargs)


class DeclaredHelocRateWinsTest(unittest.TestCase):
    """The core #654 fix: a declared heloc_rate reaches the engine's HELOC
    path outright, never derived from the mortgage rate."""

    def test_heloc_path_reports_the_declared_rate_not_the_mortgage_rate(self):
        config = _make_config(mortgage_rate=0.02, heloc_rate=0.05)
        sim = _sim(config)
        self.assertEqual(sim.heloc_path.get_heloc_rate(0), 0.05)
        self.assertNotEqual(sim.heloc_path.get_heloc_rate(0), config.mortgage_rate)

    def test_year_result_heloc_rate_matches_the_declared_value(self):
        config = _make_config(mortgage_rate=0.02, heloc_rate=0.05)
        sim = _sim(config, lump_sum=50_000)
        results = sim.run()
        self.assertEqual(results[0].heloc_rate, 0.05)

    def test_differing_rate_produces_different_heloc_interest_than_equal_rate(self):
        """The required behavioural assertion (#654's enforcement ask): a
        document whose HELOC rate differs from its mortgage rate MUST
        produce different HELOC interest than one where they are equal --
        an assertion that survives even if some future citation-checking
        guard is satisfied by an untrue citation again."""
        drawn = 50_000.0

        equal_cfg = _make_config(mortgage_rate=0.02, heloc_rate=0.02)
        differs_cfg = _make_config(mortgage_rate=0.02, heloc_rate=0.05)

        equal_results = _sim(equal_cfg, lump_sum=drawn).run()
        differs_results = _sim(differs_cfg, lump_sum=drawn).run()

        equal_balance = equal_results[0].heloc_balance
        differs_balance = differs_results[0].heloc_balance
        self.assertNotEqual(
            equal_balance, differs_balance,
            "A HELOC rate that differs from the mortgage rate must produce "
            "different HELOC interest than one where they are equal -- "
            "identical balances mean the declared HELOC rate never reached "
            "the engine (#654).",
        )
        # Precisely: both configs draw the identical $50k and share every
        # other input (including any RRSP-refund paydown, which depends on
        # income/marginal rate, not heloc_rate) -- so the balances' entire
        # difference is exactly one year of interest at the rate spread.
        self.assertAlmostEqual(
            differs_balance - equal_balance,
            drawn * (0.05 - 0.02),
            places=2,
        )

    def test_undeclared_heloc_rate_on_a_drawing_heloc_logs_a_warning(self):
        """DP#32: absence must fail loudly, not silently default. A
        hand-built config that will actually draw the HELOC (a lump_sum
        margin draw here) but never declared heloc_rate must surface that
        gap, not silently approximate one from the mortgage and say
        nothing (#654). Logged (not raised/warnings.warn'd): this repo's
        pytest config treats warnings.warn as a hard failure, which would
        make every SM/margin-draw test built before heloc_rate existed
        fail outright rather than surface the gap."""
        config = _make_config(mortgage_rate=0.02, heloc_rate=None)
        with self.assertLogs("simulation", level="WARNING") as caught:
            _sim(config, lump_sum=50_000)
        self.assertTrue(
            any("heloc_rate" in m and "654" in m for m in caught.output),
            f"Expected a loud #654 warning about the undeclared HELOC rate; got: {caught.output}",
        )

    def test_no_draw_no_warning_even_without_a_declared_rate(self):
        """The DP#32 warning is scoped to when the HELOC will actually
        carry a balance -- a household that never draws its margin and
        never enables SM readvance must not be warned about a rate that
        will never price anything."""
        config = _make_config(mortgage_rate=0.02, heloc_rate=None)
        with self.assertNoLogs("simulation", level="WARNING"):
            _sim(config)  # no lump_sum, heloc_readvance=False


class ContractDrivenHelocRateTest(unittest.TestCase):
    """End-to-end reproduction of #654's own issue-body snippet, now fixed:
    a real (fabricated) contract document's HELOC rate reaches the engine."""

    def setUp(self):
        with open(contract_schema.EXAMPLE_PATH) as f:
            doc = json.load(f)
        # Trim to the two-adults-plus-children sub-family the legacy engine
        # can represent (same fixture-shrinking helper test_input_contract.py
        # uses) -- schema/example.json is itself a fabricated household
        # (DP#15).
        doc = copy.deepcopy(doc)
        keep = {"p1", "p2", "ca", "cb"}
        doc["people"] = [p for p in doc["people"] if p["id"] in keep]
        for p in doc["people"]:
            p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]

        def owner_ids(owner):
            return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}

        doc["accounts"] = [a for a in doc["accounts"] if owner_ids(a["owner"]) <= keep]
        doc["liabilities"] = [l for l in doc["liabilities"] if owner_ids(l["owner"]) <= keep]
        doc["properties"] = [p for p in doc["properties"] if owner_ids(p["owner"]) <= keep]
        doc["estate"]["rollover_overrides"] = [
            o for o in doc["estate"]["rollover_overrides"]
            if o["account"] in {a["id"] for a in doc["accounts"]}
        ]
        doc["estate"]["life_insurance"] = [i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
        doc["assumptions"]["mortality"] = [m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
        contract_schema.validate_contract(doc)
        self.doc = doc

    def test_issue_body_repro_is_fixed(self):
        """#654's issue body: ``cfg['property'].get('heloc_rate')`` used to
        be None even though the contract declared one. It must not be now."""
        cfg = ic.load_and_map(self._write_and_get_path())
        self.assertIsNotNone(cfg['property'].get('heloc_rate'))
        self.assertEqual(cfg['property']['heloc_rate'], 0.0545)
        # legacy['heloc'] stays deleted (DP#9) -- that was never the gap.
        self.assertIsNone(cfg.get('heloc'))

    def _write_and_get_path(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self.doc, f)
        self.addCleanup(os.remove, path)
        return path

    def test_engine_heloc_path_uses_the_contract_declared_rate(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.heloc_rate, 0.0545)
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
        self.assertEqual(sim.heloc_path.get_heloc_rate(0), 0.0545)
        # The fixture's mortgage (0.045) and HELOC (0.0545) rates differ by
        # construction -- the engine must not conflate them.
        self.assertNotEqual(sim.heloc_path.get_heloc_rate(0), sim.rate_path.get_rate(0))


if __name__ == "__main__":
    unittest.main()
