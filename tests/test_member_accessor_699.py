"""Issue #699: SimulationConfig members must be reachable by a stable *entity*
identity through a single accessor seam, not by scattered `role ==` string
lookups.

This is the transitional seam the multi-generation rewrite (#643) builds on:
`adults()` / `member_by_role()` / `member_by_id()` centralize "which dict is
this person" so later steps can swap the resolution from a role string to a
relationship-graph traversal in ONE place instead of ~20. For the two-adult
household the seam must resolve to exactly the same member objects the old
`next(m for m in family_members if m['role'] == ...)` idiom returned -- this is
a representation change, not a behaviour change (recon golden invariant guards
the behaviour half; these tests guard the representation half).
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import input_contract as ic
from simulation_config import (
    SimulationConfig,
    find_member_by_role,
    adult_members,
)
from test_input_contract import _load_example, _two_generation_subset


def _two_adult_config() -> SimulationConfig:
    return SimulationConfig.from_dict({
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000},
                {'role': 'spouse', 'birth_year': 1982, 'gross_income': 80_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': 2015}],
        },
        'assumptions': {'start_year': 2026, 'projection_years': 3},
    })


class AdultsAccessorTest(unittest.TestCase):
    def test_adults_yields_primary_then_spouse_in_order(self):
        cfg = _two_adult_config()
        adults = cfg.adults()
        self.assertEqual([m['role'] for m in adults], ['primary', 'spouse'])

    def test_adults_returns_the_same_member_objects_as_role_lookup(self):
        cfg = _two_adult_config()
        primary = next(m for m in cfg.family_members if m['role'] == 'primary')
        spouse = next(m for m in cfg.family_members if m['role'] == 'spouse')
        self.assertIs(cfg.adults()[0], primary)
        self.assertIs(cfg.adults()[1], spouse)

    def test_adults_excludes_children(self):
        cfg = _two_adult_config()
        self.assertNotIn('child', [m['role'] for m in cfg.adults()])


class MemberByRoleTest(unittest.TestCase):
    def test_resolves_primary_and_spouse(self):
        cfg = _two_adult_config()
        self.assertEqual(cfg.member_by_role('primary')['gross_income'], 120_000)
        self.assertEqual(cfg.member_by_role('spouse')['gross_income'], 80_000)

    def test_absent_role_returns_the_supplied_default(self):
        cfg = _two_adult_config()
        self.assertIsNone(cfg.member_by_role('grandparent'))
        sentinel = {}
        self.assertIs(cfg.member_by_role('grandparent', sentinel), sentinel)


class MemberByIdTest(unittest.TestCase):
    def test_directly_constructed_member_gets_a_stable_id(self):
        """A config built via from_dict without explicit ids still exposes a
        stable per-member id (the role label is the fallback identity in the
        two-adult world), so member_by_id round-trips."""
        cfg = _two_adult_config()
        primary = cfg.member_by_role('primary')
        self.assertIn('id', primary)
        self.assertIs(cfg.member_by_id(primary['id']), primary)

    def test_id_from_the_input_contract_is_the_person_id_and_round_trips(self):
        doc = _two_generation_subset(_load_example())
        internal = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(internal)
        # p1/p2 are the two adults' schema person ids -- they must survive the
        # contract -> internal -> SimulationConfig hop unchanged (#699/#785).
        self.assertEqual(cfg.member_by_role('primary')['id'], 'p1')
        self.assertEqual(cfg.member_by_role('spouse')['id'], 'p2')
        self.assertIs(cfg.member_by_id('p1'), cfg.member_by_role('primary'))
        self.assertIs(cfg.member_by_id('p2'), cfg.member_by_role('spouse'))


class ModuleLevelSeamTest(unittest.TestCase):
    """The raw-member-list helpers the functions operating on a bare
    `members` list (optimize/scenario/output) route through -- same seam, no
    SimulationConfig instance required."""

    def test_find_member_by_role_matches_next_idiom(self):
        members = [
            {'role': 'primary', 'gross_income': 1},
            {'role': 'spouse', 'gross_income': 2},
        ]
        self.assertIs(find_member_by_role(members, 'primary'), members[0])
        self.assertIs(find_member_by_role(members, 'spouse'), members[1])

    def test_find_member_by_role_default_on_absence(self):
        self.assertIsNone(find_member_by_role([], 'primary'))
        empty = {}
        self.assertIs(find_member_by_role([], 'primary', empty), empty)

    def test_adult_members_is_primary_then_spouse(self):
        members = [
            {'role': 'spouse', 'gross_income': 2},
            {'role': 'child', 'name': 'c'},
            {'role': 'primary', 'gross_income': 1},
        ]
        self.assertEqual([m['role'] for m in adult_members(members)],
                         ['primary', 'spouse'])


if __name__ == '__main__':
    unittest.main()
