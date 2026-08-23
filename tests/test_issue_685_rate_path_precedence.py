#!/usr/bin/env python3
"""Issue #685: a stale `rate_paths` BELIEF silently overrode the SIGNED rate.

The bug
-------
A household signs a mortgage at a known contractual rate and declares it, off
the document, on `liabilities[kind=mortgage].rate`. It also carries an
`assumptions.rate_paths` block -- a belief about what borrowing costs over the
horizon. `input_contract.to_internal_config` mapped `rate_paths.heloc.rate`
onto `assumptions.heloc_rate`, which is `config_access.resolve_heloc_rate`'s
FIRST tier -- the DECISION channel, deliberately built to outrank the
household's own declared rate so that an anchor scenario ("what if the HELOC
reprices on renewal", DP#5) is not shadowed by it.

So the belief inherited the decision's authority. Every optimizer, ranking and
reporting consumer priced the Smith-Manoeuvre spread off the belief, while
`simulation.py`'s engine -- which reads tier 2 only -- charged the real rate.
One run, two different HELOC rates, and nothing anywhere said so. In the case
that surfaced this, the belief was the household's PREVIOUS lender's rate, and
44 years were projected at a cost of debt that contradicted the contract the
engine had loaded one function earlier.

The rule this file pins
-----------------------
**A rate declared on a liability is a FACT and wins at year zero, always.** A
`rate_paths` entry is a BELIEF: it describes what the borrowing costs AFTER the
current term, and it may never reprice a rate the contract has already pinned.
When the two disagree about year zero that is a contradiction in the user's own
input, and it is reported -- loudly at load, and durably as a `model_fidelity`
Approximation naming the liability, both rates, and which one won. Silence is
the one outcome that must be impossible (AGENTS.md; DP#32).

DP#15/DP#4: every figure below is fabricated, round, role-based.

Run: uv run pytest tests/test_issue_685_rate_path_precedence.py -q
"""

import copy
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract as ic
import model_fidelity
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from config_access import resolve_heloc_rate
from simulation_config import SimulationConfig

from test_input_contract import _load_example, _two_generation_subset

# The signed contract. Fabricated, round, and deliberately NOT equal to each
# other or to any belief below -- so a rate that leaks from the wrong place is
# unmistakable.
SIGNED_MORTGAGE_RATE = 0.0370
SIGNED_HELOC_RATE = 0.0470
# The stale belief. In the household that surfaced #685 this was the PREVIOUS
# lender's rate, 125bp above the mortgage actually signed.
STALE_BELIEF_RATE = 0.0495


def _doc(mortgage_path=None, heloc_path=None,
         signed_mortgage=SIGNED_MORTGAGE_RATE, signed_heloc=SIGNED_HELOC_RATE):
    """A contract document with SIGNED rates on the liabilities and whatever
    `rate_paths` belief the caller wants to argue with them."""
    doc = _two_generation_subset(_load_example())
    for liab in doc["liabilities"]:
        if liab["kind"] == "mortgage":
            liab["rate"] = signed_mortgage
        elif liab["kind"] == "heloc":
            liab["rate"] = signed_heloc
            liab["rate_type"] = "fixed"
    doc["assumptions"]["rate_paths"] = {
        "mortgage": mortgage_path or {"type": "fixed", "rate": signed_mortgage},
        "heloc": heloc_path or {"type": "fixed", "rate": signed_heloc},
    }
    ic.validate_contract(doc)
    return doc


def _year0_engine_rates(cfg):
    """(mortgage, heloc) rate the ENGINE actually charges in year 0."""
    sim_cfg = SimulationConfig.from_dict(copy.deepcopy(cfg))
    sim_cfg.projection_years = 1
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg))
    return (sim.rate_path.get_rate(0),
            sim.heloc_path.get_heloc_rate(0, sim.rate_path.rate_type))


# Every shape a rate_path can take (schema `$defs/rate_path`), each asserting a
# year-zero rate that CONTRADICTS the signed one.
_CONTRADICTING_PATHS = {
    "fixed": {"type": "fixed", "rate": STALE_BELIEF_RATE},
    "variable": {"type": "variable", "path": [STALE_BELIEF_RATE, 0.06, 0.06]},
    "forecast": {"type": "forecast", "path": [STALE_BELIEF_RATE, 0.055]},
}


class DeclaredRateWinsAtYearZeroTest(unittest.TestCase):
    """THE invariant: for any liability with a declared rate, the effective rate
    used in year 0 equals the declared rate -- regardless of `rate_paths`."""

    def test_declared_rate_wins_over_every_contradicting_rate_path_shape(self):
        for shape, path in _CONTRADICTING_PATHS.items():
            with self.subTest(rate_path_shape=shape):
                cfg = ic.to_internal_config(
                    _doc(mortgage_path=copy.deepcopy(path),
                         heloc_path=copy.deepcopy(path)))

                # The internal config carries the FACT, not the belief.
                self.assertEqual(cfg["property"]["mortgage_rate"], SIGNED_MORTGAGE_RATE)
                self.assertEqual(cfg["property"]["heloc_rate"], SIGNED_HELOC_RATE)

                # The optimizer/ranking/reporting consumers (#677's canonical
                # resolver) -- this is the tier the belief used to hijack.
                self.assertEqual(resolve_heloc_rate(cfg), SIGNED_HELOC_RATE)
                self.assertNotIn("heloc_rate", cfg["assumptions"],
                                 "the contract loader must not write the DECISION "
                                 "channel (assumptions.heloc_rate) from a BELIEF (#685)")

                # And the engine that actually charges the interest.
                mortgage_rate, heloc_rate = _year0_engine_rates(cfg)
                self.assertEqual(mortgage_rate, SIGNED_MORTGAGE_RATE)
                self.assertEqual(heloc_rate, SIGNED_HELOC_RATE)

    def test_optimizer_and_engine_agree_on_one_rate(self):
        """The #685 signature: the run reported one HELOC rate and charged
        another. The two views must be the same number."""
        cfg = ic.to_internal_config(
            _doc(heloc_path={"type": "fixed", "rate": STALE_BELIEF_RATE}))
        _, engine_rate = _year0_engine_rates(cfg)
        self.assertEqual(resolve_heloc_rate(cfg), engine_rate)

    def test_a_belief_that_agrees_changes_nothing(self):
        """Control: the same document with a CONSISTENT rate_paths block runs
        the same rates. The fix must not be an artefact of the disagreement."""
        cfg = ic.to_internal_config(_doc())  # paths default to the signed rates
        self.assertEqual(resolve_heloc_rate(cfg), SIGNED_HELOC_RATE)
        self.assertEqual(_year0_engine_rates(cfg),
                         (SIGNED_MORTGAGE_RATE, SIGNED_HELOC_RATE))


class ContradictionIsNeverSilentTest(unittest.TestCase):
    """"The right one happened to win" is half a fix. A belief and a fact that
    disagree about the same rate for the same period is a contradiction in the
    input, and the run must say so."""

    def test_load_warns_naming_the_liability_both_rates_and_the_winner(self):
        with self.assertLogs("input_contract", level=logging.WARNING) as caught:
            ic.to_internal_config(
                _doc(heloc_path={"type": "fixed", "rate": STALE_BELIEF_RATE}))
        blob = "\n".join(caught.output)
        self.assertIn("heloc_main", blob)          # the liability, named
        self.assertIn("4.95%", blob)               # the belief
        self.assertIn("4.70%", blob)               # the signed fact
        self.assertIn("WINS", blob)                # and which one won

    def test_contradiction_is_recorded_on_the_config(self):
        cfg = ic.to_internal_config(
            _doc(mortgage_path={"type": "fixed", "rate": STALE_BELIEF_RATE}))
        conflicts = model_fidelity.rate_path_conflicts(cfg)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0], {
            "liability_id": "mortgage_main",
            "liability_kind": "mortgage",
            "declared_rate": SIGNED_MORTGAGE_RATE,
            "believed_rate": STALE_BELIEF_RATE,
            "winner": "declared",
        })

    def test_approximation_surfaces_and_names_both_figures(self):
        cfg = ic.to_internal_config(
            _doc(heloc_path={"type": "fixed", "rate": STALE_BELIEF_RATE}))
        block = model_fidelity.to_dict(cfg, "max_net_benefit")
        entry = next(a for a in block["approximations"]
                     if a["id"] == "rate_path_contradicts_signed_rate")
        findings = "\n".join(entry["findings"])
        self.assertIn("heloc_main", findings)
        self.assertIn("4.70%", findings)
        self.assertIn("4.95%", findings)

        console = "\n".join(model_fidelity.render_text(cfg, "max_net_benefit"))
        self.assertIn("4.70%", console)
        self.assertIn("4.95%", console)

    def test_every_report_surface_names_the_figures(self):
        """A caveat that gestures at "a contradiction" without saying WHICH
        liability and WHICH two rates is not a disclosure. Every surface that
        renders the fidelity section must carry the run's own numbers -- the
        HTML card builds its own rows and had to be wired separately."""
        from output_plugins import HtmlReport, JsonReport, TextReport

        cfg = ic.to_internal_config(
            _doc(heloc_path={"type": "fixed", "rate": STALE_BELIEF_RATE}))
        results = [{"name": "base", "net_benefit": 0.0}]
        for report in (TextReport(results, cfg),
                       JsonReport(results, cfg),
                       HtmlReport(results, cfg)):
            with self.subTest(surface=type(report).__name__):
                rendered = report.render()
                self.assertIn("heloc_main", rendered)
                self.assertIn("4.70%", rendered)
                self.assertIn("4.95%", rendered)

    def test_a_consistent_document_raises_no_caveat(self):
        """A caveat that fires when there is nothing wrong teaches the reader to
        skim the section -- as corrosive as a missing one (model_fidelity's own
        stale-caveat doctrine)."""
        cfg = ic.to_internal_config(_doc())
        self.assertEqual(model_fidelity.rate_path_conflicts(cfg), [])
        active = {a.id for a in model_fidelity.active_approximations(cfg)}
        self.assertNotIn("rate_path_contradicts_signed_rate", active)
        with self.assertNoLogs("input_contract", level=logging.WARNING):
            ic.to_internal_config(_doc())


class DecisionChannelStillOutranksTheFactTest(unittest.TestCase):
    """#685 narrows tier 1 to DECISIONS; it must not break them (DP#5/DP#18).

    A deliberate anchor override ("assume the HELOC reprices on renewal") is the
    user asking a hypothetical, and it still wins over the declared rate. That
    is the whole distinction the fix rests on: a BELIEF may not displace a fact,
    a DECISION may.
    """

    def test_anchor_override_still_wins(self):
        cfg = ic.to_internal_config(_doc())
        self.assertEqual(resolve_heloc_rate(cfg), SIGNED_HELOC_RATE)
        cfg["assumptions"]["heloc_rate"] = 0.08   # a posed scenario, not a belief
        self.assertEqual(resolve_heloc_rate(cfg), 0.08)


class RatePathYearZeroTest(unittest.TestCase):
    """`_rate_path_year0` -- which leaf carries the belief's year-zero claim."""

    def test_fixed_asserts_its_rate(self):
        self.assertEqual(
            ic._rate_path_year0({"type": "fixed", "rate": 0.05}), 0.05)

    def test_variable_and_forecast_assert_the_first_element(self):
        self.assertEqual(
            ic._rate_path_year0({"type": "variable", "path": [0.05, 0.06]}), 0.05)
        self.assertEqual(
            ic._rate_path_year0({"type": "forecast", "path": [0.04, 0.06]}), 0.04)

    def test_an_empty_path_asserts_nothing_and_is_not_zero(self):
        """DP#32: absence is absence. An empty path makes no claim about year
        zero, and must not be read as a claim that the rate is 0%."""
        self.assertIsNone(ic._rate_path_year0({"type": "variable", "path": []}))

    def test_a_zero_belief_is_a_real_belief_that_contradicts(self):
        """DP#32 the other way: 0% is a value, not absence. A rate_path
        asserting 0% against a signed 4.70% is a contradiction, not a no-op."""
        conflicts = ic._reconcile_rate_paths(
            {"heloc": {"type": "fixed", "rate": 0.0}},
            {"heloc": {"id": "heloc_main", "rate": SIGNED_HELOC_RATE}},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["believed_rate"], 0.0)


class NoLiabilityToContradictTest(unittest.TestCase):
    """A rate_path is free to supply the rate where the contract does NOT pin
    it -- that is the only coherent reading of a path with no matching signed
    liability, and it is not a contradiction."""

    def test_no_declared_liability_means_no_conflict(self):
        conflicts = ic._reconcile_rate_paths(
            {"mortgage": {"type": "fixed", "rate": 0.0495}},
            {"mortgage": None},
        )
        self.assertEqual(conflicts, [])


if __name__ == "__main__":
    unittest.main()
