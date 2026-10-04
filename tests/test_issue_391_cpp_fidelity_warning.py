#!/usr/bin/env python3
"""Issue #391: a CPP/QPP pension modelled as $0 must not be silent.

`contract_people` has logged this at load time since #389/#390, but it logs to
a logger nothing configures: a household with no Service Canada / Retraite
Quebec Statement, no `earnings_history` and no employment income reads a plan
whose first-pillar retirement income is silently zero. Issue #391 asks for a
`model_fidelity`-style warning, and this is the output-surface half of that
fix -- `model_fidelity.cpp_modelled_as_zero`.

Two halves are tested, deliberately at different levels (DP#11):

- the PREDICATE (`cpp_modelled_as_zero_people`) as a unit, over hand-built
  configs. This is where the two distinct causes get pinned -- no CPP source
  at all, versus a declared source whose estimate computes to $0 -- because
  they need different fixes and a reader who cannot tell them apart cannot
  act. It also pins the "cannot read this config" shapes to an empty list, so
  an unreadable config can never invent an age to judge (DP#32);
- the CAVEAT end to end over the real adapter
  (`input_contract.to_internal_config`), because the caveat reads the
  adapter's OWN outputs (`cpp_benefit_source` / `cpp_monthly_estimated`) and a
  test that hand-builds that dict instead would be testing a fiction (DP#11).

The negative cases are load-bearing in the other direction: a false caveat is
worse than none, because a registry whose entries are wrong teaches its reader
to ignore the entries that are still true. So the estimate-present, the
too-young, and the shipped-example cases must all stay SILENT.

DP#15: fabricated round numbers and role-based names only.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import model_fidelity
from test_input_contract import _load_example, _two_generation_subset


# ── Unit level: the predicate over hand-built configs ─────────────────────

def _cfg(members, start_year=2026):
    """A minimal mapped-config shape, as `to_internal_config` produces it."""
    return {'assumptions': {'start_year': start_year},
            'family': {'members': members}}


class PredicateReadsTheAdaptersOwnOutputs(unittest.TestCase):
    def test_no_cpp_source_is_reported_with_no_source(self):
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                         [{'role': 'primary', 'age': 55, 'source': None}])

    def test_zero_estimate_is_reported_as_a_zero_estimate(self):
        """`cpp_benefit_source` set but no amount: the adapter DID look and
        found nothing contributory. Same headline, different fix -- the
        findings must not claim 'no Statement' here."""
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                     'cpp_benefit_source': 'estimated_from_incomes'}])
        people = model_fidelity.cpp_modelled_as_zero_people(cfg)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]['source'], 'estimated_from_incomes')

    def test_a_positive_estimate_is_never_reported(self):
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                     'cpp_benefit_source': 'estimated_from_incomes',
                     'cpp_monthly_estimated': 1500.0}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg), [])

    def test_below_the_age_gate_is_not_reported(self):
        """A younger adult has contributions left to make, so an absent CPP
        source is a detail rather than an omission from the plan."""
        young = model_fidelity.CPP_FIDELITY_MIN_AGE - 5
        cfg = _cfg([{'role': 'primary',
                     'birth_year': 2026 - young}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg), [])

    def test_exactly_at_the_age_gate_is_reported(self):
        cfg = _cfg([{'role': 'primary',
                     'birth_year': 2026 - model_fidelity.CPP_FIDELITY_MIN_AGE}])
        self.assertEqual(len(model_fidelity.cpp_modelled_as_zero_people(cfg)), 1)

    def test_undatable_age_is_not_reported(self):
        """No birth year means no age, and an invented one would be exactly
        the silent guess this caveat exists to end (DP#32)."""
        cfg = _cfg([{'role': 'primary'}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg), [])

    def test_missing_role_is_labelled_not_dropped(self):
        """A member with no usable role label is still REPORTED, under a
        neutral label -- dropping it would recreate exactly the silence this
        predicate exists to end."""
        cfg = _cfg([{'birth_year': 1971}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                         [{'role': 'adult', 'age': 55, 'source': None}])
        cfg = _cfg([{'role': '', 'birth_year': 1971}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                         [{'role': 'adult', 'age': 55, 'source': None}])

    def test_a_malformed_amount_is_declined_not_called_zero(self):
        """Cite (#391 review): a non-numeric `cpp_monthly_estimated` is not
        evidence of a $0 pension. Reporting one would state a figure this
        predicate never verified -- and a false caveat is worse than none.
        `bool` is excluded too: True must not pass as "has an amount"."""
        for bad in ('1200', [1200], {'monthly': 1200}, True, False):
            cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                         'cpp_monthly_estimated': bad}])
            self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                             [], msg=f"{bad!r} must not be read as $0")

    def test_a_present_but_null_amount_IS_the_zero_case(self):
        """Absent or None is the real $0: the engine's own reader turns a
        missing or null cpp_monthly_estimated into 0 as well."""
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                     'cpp_monthly_estimated': None}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                         [{'role': 'primary', 'age': 55, 'source': None}])

    def test_a_zero_amount_is_reported(self):
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                     'cpp_benefit_source': 'statement',
                     'cpp_monthly_estimated': 0}])
        people = model_fidelity.cpp_modelled_as_zero_people(cfg)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]['source'], 'statement')

    def test_a_malformed_member_is_skipped_not_fatal(self):
        """A non-dict entry in `family.members` is skipped, not fatal: the
        caveat is a disclosure and must never take a run down."""
        cfg = _cfg(['not a member', {'role': 'primary', 'birth_year': 1971}])
        self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                         [{'role': 'primary', 'age': 55, 'source': None}])

    def test_unreadable_config_shapes_are_empty_not_guessed(self):
        for cfg in ({}, {'family': {}}, {'family': {'members': 'nope'}},
                    {'family': {'members': []}},
                    {'family': {'members': [{'role': 'primary', 'birth_year': 1970}]}},
                    # assumptions present but the year is missing, not a
                    # number, or a bool -- no age can be read off any of them.
                    {'assumptions': {},
                     'family': {'members': [{'role': 'primary', 'birth_year': 1970}]}},
                    {'assumptions': {'start_year': '2026'},
                     'family': {'members': [{'role': 'primary', 'birth_year': 1970}]}},
                    {'assumptions': {'start_year': True},
                     'family': {'members': [{'role': 'primary', 'birth_year': 1970}]}},
                    {'assumptions': 'nope', 'family': {'members': []}},
                    None, 'not a config'):
            self.assertEqual(model_fidelity.cpp_modelled_as_zero_people(cfg),
                             [], msg=f"must not invent a finding from {cfg!r}")


class FindingsNameTheAdultAndTheCause(unittest.TestCase):
    def _findings(self, cfg):
        ctx = model_fidelity.FidelityContext(cfg=cfg, objective_name=None)
        return model_fidelity._describe_cpp_modelled_as_zero(ctx)

    def test_no_source_names_what_to_supply(self):
        lines = self._findings(_cfg([{'role': 'primary', 'birth_year': 1971}]))
        self.assertEqual(len(lines), 1)
        self.assertIn('primary', lines[0])
        self.assertIn('55', lines[0])
        self.assertIn('entitlements.cpp', lines[0])
        self.assertIn('earnings_history', lines[0])

    def test_zero_estimate_does_not_claim_the_statement_is_missing(self):
        lines = self._findings(_cfg([{'role': 'spouse', 'birth_year': 1971,
                                      'cpp_benefit_source': 'estimated_from_incomes'}]))
        self.assertIn('estimated_from_incomes', lines[0])
        self.assertIn('$0', lines[0])
        self.assertNotIn('no benefits.cpp', lines[0])

    def test_no_findings_when_nothing_is_affected(self):
        cfg = _cfg([{'role': 'primary', 'birth_year': 1971,
                     'cpp_monthly_estimated': 1200.0}])
        self.assertEqual(self._findings(cfg), [])


# ── End to end: the adapter's decision drives the caveat ──────────────────

def _near_retirement_without_cpp_source():
    """The shipped two-generation example with the primary aged to 55 and
    stripped of every CPP source: no Statement, no history, and incomes
    neutered so always-on estimation (#390) cannot produce an amount."""
    doc = _two_generation_subset(_load_example())
    p1 = next(p for p in doc["people"] if p["id"] == "p1")
    p1.pop("entitlements", None)
    p1.pop("earnings_history", None)
    benefits = p1.get("benefits") or {}
    benefits.pop("cpp", None)
    if benefits:
        p1["benefits"] = benefits
    else:
        p1.pop("benefits", None)
    for inc in p1.get("incomes", []):
        inc["kind"] = "other"
    p1["birth_date"] = "1971-03-14"
    return doc


def _active_ids(cfg):
    return {a.id for a in model_fidelity.active_approximations(cfg, None)}


class CaveatFollowsTheAdapter(unittest.TestCase):
    def test_caveat_is_active_and_names_the_adult(self):
        cfg = ic.to_internal_config(_near_retirement_without_cpp_source())
        self.assertIn('cpp_modelled_as_zero', _active_ids(cfg))
        approx = next(a for a in model_fidelity.active_approximations(cfg, None)
                      if a.id == 'cpp_modelled_as_zero')
        findings = approx.findings_for(
            model_fidelity.FidelityContext(cfg=cfg, objective_name=None))
        self.assertTrue(any('primary' in line for line in findings),
                        msg=f"expected the adult named in {findings}")

    def test_caveat_disappears_once_an_estimate_exists(self):
        """Always-on estimation (#390) gives this earner a real pension, so
        the caveat must NOT fire -- a false caveat is corrosive."""
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        p1["birth_date"] = "1971-03-14"   # 55: past the age gate
        cfg = ic.to_internal_config(doc)
        primary = next(m for m in cfg["family"]["members"]
                       if m["role"] == "primary")
        self.assertGreater(primary["cpp_monthly_estimated"], 0)
        self.assertNotIn('cpp_modelled_as_zero', _active_ids(cfg))

    def test_younger_adult_without_a_cpp_source_stays_silent(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        for inc in p1.get("incomes", []):
            inc["kind"] = "other"
        p1["birth_date"] = "1986-03-14"   # 40: years of contributions left
        self.assertNotIn('cpp_modelled_as_zero',
                         _active_ids(ic.to_internal_config(doc)))

    def test_shipped_examples_do_not_trip_the_caveat(self):
        """Guards examples/** against a new report.

        `tests/test_examples_guard.py` compares the committed `report.*`
        against a fresh run, so a caveat that fired for a shipped example
        would fail that guard until the reports were regenerated under
        Python 3.12. Both shipped documents give every adult declared
        employment income, so always-on estimation (#390) yields a real
        pension and the caveat cannot fire. Asserted here rather than
        assumed, because a false caveat is the failure this registry is
        most sensitive to.
        """
        import glob
        import json
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(
            root, 'examples', '*', '*', 'input.json')))
        self.assertTrue(paths, msg="no shipped example inputs found")
        for path in paths:
            with open(path) as fh:
                doc = json.load(fh)
            with self.subTest(example=os.path.basename(os.path.dirname(path))):
                self.assertNotIn('cpp_modelled_as_zero',
                                 _active_ids(ic.to_internal_config(doc)))


class EntryIsWellFormed(unittest.TestCase):
    def test_registered_with_a_named_figure_and_direction(self):
        entry = next(a for a in model_fidelity.all_approximations()
                     if a.id == 'cpp_modelled_as_zero')
        self.assertTrue(entry.biased_figure)
        self.assertIsInstance(entry.direction, model_fidelity.Direction)
        self.assertEqual(entry.issue, '#391')

    def test_rendered_into_the_report_surfaces(self):
        """The whole point: the caveat is not just registered, it reaches the
        text/JSON rendering every report uses."""
        cfg = ic.to_internal_config(_near_retirement_without_cpp_source())
        entry = next(a for a in model_fidelity.all_approximations()
                     if a.id == 'cpp_modelled_as_zero')
        text = "\n".join(model_fidelity.render_text(cfg, None))
        self.assertIn(entry.summary, text)
        self.assertIn('#391', text)
        payload = model_fidelity.to_dict(cfg, None)
        self.assertIn('cpp_modelled_as_zero',
                      [a['id'] for a in payload['approximations']])
        rendered = next(a for a in payload['approximations']
                        if a['id'] == 'cpp_modelled_as_zero')
        self.assertTrue(rendered.get('findings'))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
