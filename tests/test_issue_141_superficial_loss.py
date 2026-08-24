#!/usr/bin/env python3
"""Issue #141: the superficial-loss rule (ITA s.53(1)(c)/s.54) -- the
anti-avoidance precondition for any tax-loss-harvest feature.

Under s.54/s.53(1)(c) a realized capital loss is DENIED when, within 30
calendar days before or after the disposition, the taxpayer OR AN AFFILIATED
PERSON (the spouse, in this engine's household) acquired the same or identical
property and still holds it 30 days after the sale. The denial is not
destruction (s.53(1)(f)): the denied loss is ADDED TO THE ACB of the
substituted property, deferring the benefit to its eventual disposition.

This file verifies, layer by layer (DP#11):

1. The PURE rule (``superficial_loss.py``): both sides of every threshold --
   |days| <= 30 in the window, endpoints included; outside it the loss is
   fully allowed; sold-out-before-day-30 likewise; a negative loss magnitude
   refuses loudly (DP#32).
2. The CONTRACT mapping: declared events ride family.members wholesale;
   an unresolvable ``acquired_by``, a declaration on a non-member, or a
   non-positive loss is refused loudly at load.
3. The FOLD wiring: an engine-run household that sells property $100,000
   below ACB and declares a spouse reacquisition inside the window books
   ZERO carry-forward pool from that loss (the `capital_loss_carryforward`
   rule nets the denial out) and carries the $100,000 on the non-reg ACB
   instead; the not-superficial side of every condition leaves the #140
   behavior byte-identical; declaring more loss than was realized refuses;
   the golden household is untouched.

The engine holds no per-lot acquisition ledger, so the window is DECLARABLE
(``people[].superficial_losses[]``), never auto-detected -- disclosed on
every run the feature activates (model_fidelity, issue #141 entry). All
figures are fabricated round numbers (DP#4/DP#15); no real personal data
enters this repo.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contract_errors import ContractAdaptationError
from superficial_loss import WINDOW_DAYS, acquisition_in_window, denied_loss, is_superficial

# ── shared fixture constants (fabricated round numbers, DP#4) ──────────────
_LOSS = 100_000.0        # pre-inclusion capital loss on the disposition
_COTTAGE_VALUE = 500_000
_COTTAGE_ACB = 600_000   # $100,000 BELOW value: a realizable loss
_SALE_YEAR = 2028
_SALE_YEAR_INDEX = 2     # projections start 2026


class TestPureSuperficialRule(unittest.TestCase):
    """The s.54 test itself: window endpoints, holding condition, refusals."""

    def test_window_endpoints_are_inclusive(self):
        self.assertTrue(acquisition_in_window(-WINDOW_DAYS))
        self.assertTrue(acquisition_in_window(0))
        self.assertTrue(acquisition_in_window(WINDOW_DAYS))
        self.assertFalse(acquisition_in_window(-WINDOW_DAYS - 1))
        self.assertFalse(acquisition_in_window(WINDOW_DAYS + 1))

    def test_same_day_rebuy_is_superficial(self):
        self.assertTrue(is_superficial(0, True))

    def test_outside_window_is_not_superficial(self):
        self.assertFalse(is_superficial(31, True))
        self.assertFalse(is_superficial(-45, True))

    def test_sold_out_before_day_30_is_not_superficial(self):
        self.assertFalse(is_superficial(19, False))

    def test_acquisition_before_sale_counts_too(self):
        # s.54(b)(i): the window reaches BACKWARD as well -- buying the
        # substitute 10 days BEFORE the disposition and holding it denies.
        self.assertTrue(is_superficial(-10, True))
        self.assertAlmostEqual(denied_loss(_LOSS, -10, True), _LOSS)

    def test_denied_loss_amounts(self):
        self.assertAlmostEqual(denied_loss(_LOSS, 19, True), _LOSS)
        self.assertAlmostEqual(denied_loss(_LOSS, 19, False), 0.0)
        self.assertAlmostEqual(denied_loss(_LOSS, 31, True), 0.0)

    def test_negative_loss_refuses_loudly(self):
        with self.assertRaises(ValueError):
            denied_loss(-1.0, 0, True)


# ──────────────────────────────────────────────────────────────────────
# Contract mapping: declarations ride members; bad ones refuse at load.
# ──────────────────────────────────────────────────────────────────────

def _base_doc():
    from test_input_contract import _load_example, _two_generation_subset
    return _two_generation_subset(_load_example())


def _add_underwater_cottage(doc, sale_date=f"{_SALE_YEAR}-06-30"):
    doc = copy.deepcopy(doc)
    doc["properties"].append({
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": [],
        "sale": {"date": sale_date, "selling_costs": 0},
    })
    return doc


def _declare(doc, person_id="p1", **overrides):
    """Attach one superficial-loss declaration to `person_id`'s person."""
    doc = copy.deepcopy(doc)
    event = {
        "year": _SALE_YEAR,
        "loss_amount": _LOSS,
        "acquired_by": "p2",           # the SPOUSE reacquires: attribution
        "days_to_acquisition": 19,
        "still_held_30_days_after": True,
    }
    event.update(overrides)
    for p in doc["people"]:
        if p["id"] == person_id:
            p["superficial_losses"] = [event]
            break
    else:
        raise AssertionError(f"person {person_id} not in fixture")
    return doc


def _map(doc):
    import input_contract as ic
    from simulation_config import SimulationConfig
    legacy = ic.to_internal_config(doc)
    return SimulationConfig.from_dict(legacy)


def _run(doc):
    import input_contract as ic
    from simulation import FamilySimulation
    from simulation_config import SimulationConfig
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


class TestContractMapping(unittest.TestCase):
    """Declarations reach the member records; malformed ones refuse loudly."""

    def test_declaration_rides_the_member_record(self):
        cfg = _map(_declare(_add_underwater_cottage(_base_doc())))
        events = cfg.superficial_loss_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["seller"], "p1")
        self.assertEqual(e["acquired_by"], "p2")
        self.assertEqual(e["year"], _SALE_YEAR)
        self.assertEqual(e["days_to_acquisition"], 19)

    def test_no_declaration_yields_empty_events(self):
        self.assertEqual(_map(_base_doc()).superficial_loss_events(), [])

    def test_unknown_acquirer_refuses_at_load(self):
        doc = _declare(_base_doc(), acquired_by="nobody")
        with self.assertRaises(ContractAdaptationError):
            _map(doc)

    def test_declaration_on_non_member_refuses_at_load(self):
        # A non-member (e.g. a child or a declared ancestor) declaring: the
        # s.53(1)(c) denial has no tax seam through a non-member -- refused,
        # never silently dropped (DP#32).
        doc = _add_underwater_cottage(_base_doc())
        outsiders = [p["id"] for p in doc["people"]
                     if p["id"] not in ("p1", "p2")]
        if not outsiders:
            self.skipTest("fixture declares no non-member person")
        doc = _declare(doc, person_id=outsiders[0])
        with self.assertRaises(ContractAdaptationError):
            _map(doc)

    def test_zero_loss_refuses_at_load(self):
        doc = _declare(_base_doc(), loss_amount=0)
        with self.assertRaises(ContractAdaptationError):
            _map(doc)


# ──────────────────────────────────────────────────────────────────────
# Engine fold: sell the cottage $100k below ACB, spouse rebuys in-window.
# Drives FamilySimulation.run() (which enforces the invariant suite --
# including the s.53(1)(f)-aware acb_le_fmv allowance -- on every run).
# ──────────────────────────────────────────────────────────────────────

class TestDenialThroughFold(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.baseline = _run(_add_underwater_cottage(_base_doc()))
        cls.denied = _run(_declare(
            _add_underwater_cottage(_base_doc())))

    def test_denial_books_no_pool_and_bumps_the_acb(self):
        r = self.denied[_SALE_YEAR_INDEX]
        # The whole loss is DENIED: nothing joins the s.111(1)(b) pool...
        self.assertAlmostEqual(r.superficial_loss_denied, _LOSS)
        self.assertEqual(r.capital_loss_realized, 0.0)
        self.assertEqual(r.capital_loss_carryforward, 0.0)
        # ...and the denied amount lands on the substituted property's ACB
        # (s.53(1)(f)): exactly $100,000 more cost basis than the baseline
        # run in the denial year (no cash moved -- total_assets is
        # unchanged by the denial itself).
        base_r = self.baseline[_SALE_YEAR_INDEX]
        self.assertAlmostEqual(r.non_reg_acb, base_r.non_reg_acb + _LOSS)
        self.assertAlmostEqual(r.total_assets, base_r.total_assets)

    def test_years_before_the_disposition_are_untouched(self):
        for i in range(_SALE_YEAR_INDEX):
            self.assertEqual(self.denied[i].superficial_loss_denied, 0.0)
            self.assertEqual(self.denied[i].capital_loss_carryforward, 0.0)

    def test_baseline_matches_issue_140_behavior(self):
        # Without a declaration the #140 ledger is unchanged: the whole loss
        # is allowable and half of it pools (taxable basis).
        r = self.baseline[_SALE_YEAR_INDEX]
        self.assertAlmostEqual(r.capital_loss_realized, _LOSS)
        self.assertAlmostEqual(r.capital_loss_carryforward, _LOSS / 2)
        self.assertEqual(r.superficial_loss_denied, 0.0)


class TestBothSidesOfEveryCondition(unittest.TestCase):
    """A NOT-superficial declaration must leave #140 byte-identical."""

    def _run_variant(self, **overrides):
        doc = _declare(_add_underwater_cottage(_base_doc()), **overrides)
        results = _run(doc)
        baseline = _run(_add_underwater_cottage(_base_doc()))
        return results, baseline

    def test_reacquisition_outside_window_fully_allows_the_loss(self):
        results, baseline = self._run_variant(days_to_acquisition=45)
        r = results[_SALE_YEAR_INDEX]
        self.assertAlmostEqual(r.superficial_loss_denied, 0.0)
        self.assertAlmostEqual(r.capital_loss_realized, _LOSS)
        self.assertAlmostEqual(r.capital_loss_carryforward, _LOSS / 2)
        # No denial => no ACB bump => identical balance sheet to baseline.
        self.assertAlmostEqual(r.non_reg_acb,
                               baseline[_SALE_YEAR_INDEX].non_reg_acb)

    def test_sold_out_before_day_30_fully_allows_the_loss(self):
        results, baseline = self._run_variant(still_held_30_days_after=False)
        r = results[_SALE_YEAR_INDEX]
        self.assertAlmostEqual(r.superficial_loss_denied, 0.0)
        self.assertAlmostEqual(r.capital_loss_carryforward, _LOSS / 2)
        self.assertAlmostEqual(r.non_reg_acb,
                               baseline[_SALE_YEAR_INDEX].non_reg_acb)


class TestLoudRefusals(unittest.TestCase):
    """A denial larger than the year's realized losses cannot be priced."""

    def test_overdeclaration_refuses_loudly(self):
        doc = _declare(_add_underwater_cottage(_base_doc()),
                       loss_amount=200_000.0)
        with self.assertRaises(ValueError):
            _run(doc)


class TestGoldenNoOp(unittest.TestCase):
    """A household declaring nothing: byte-identical golden trajectory."""

    GOLDEN_TERMINAL_TOTAL_ASSETS = 9709753.139463063

    @classmethod
    def setUpClass(cls):
        # THE golden 46-year household fixture (issue #581) -- the one the
        # TERMINAL_TOTAL_ASSETS invariant is defined on (AGENTS.md).
        from test_golden_trajectory_581 import _run, golden_household_config
        cls.results = _run(golden_household_config())

    def test_golden_invariant_unchanged(self):
        self.assertEqual(repr(self.results[-1].total_assets),
                         repr(self.GOLDEN_TERMINAL_TOTAL_ASSETS))

    def test_whole_ledger_stays_zero(self):
        for i, r in enumerate(self.results):
            self.assertEqual(
                (r.superficial_loss_denied, r.capital_loss_realized,
                 r.capital_loss_carryforward),
                (0.0, 0.0, 0.0),
                f"year index {i}: no-declaration household must stay all-zero")


# ──────────────────────────────────────────────────────────────────────
# model_fidelity: the step-scale abstraction discloses itself when (and
# only when) the feature activates.
# ──────────────────────────────────────────────────────────────────────

class TestFidelityDisclosure(unittest.TestCase):

    def test_disclosure_active_only_for_declaring_households(self):
        import model_fidelity
        ids = {a.id for a in model_fidelity.all_approximations()}
        self.assertIn('superficial_loss_window_is_declarable', ids)
        declaring = {'family': {'members': [
            {'id': 'p1', 'role': 'primary', 'superficial_losses': [
                {'year': 2028, 'loss_amount': 100_000,
                 'acquired_by': 'p2', 'days_to_acquisition': 19,
                 'still_held_30_days_after': True}]},
            {'id': 'p2', 'role': 'spouse'},
        ]}}
        clean = {'family': {'members': [
            {'id': 'p1', 'role': 'primary'}, {'id': 'p2', 'role': 'spouse'}]}}
        active_declaring = {a.id for a in
                            model_fidelity.active_approximations(declaring)}
        active_clean = {a.id for a in
                        model_fidelity.active_approximations(clean)}
        self.assertIn('superficial_loss_window_is_declarable', active_declaring)
        self.assertNotIn('superficial_loss_window_is_declarable',
                         active_clean)

    def test_findings_name_this_runs_facts(self):
        import model_fidelity
        cfg = {'family': {'members': [
            {'id': 'p1', 'role': 'primary', 'superficial_losses': [
                {'year': 2028, 'loss_amount': 100_000,
                 'acquired_by': 'p2', 'days_to_acquisition': 19,
                 'still_held_30_days_after': True}]},
        ]}}
        approx = next(a for a in model_fidelity.all_approximations()
                      if a.id == 'superficial_loss_window_is_declarable')
        findings = approx.findings_for(model_fidelity.FidelityContext(cfg=cfg))
        self.assertEqual(len(findings), 1)
        f = findings[0]
        for expected in ('p1', 'p2', '2028', '100000', '+19'):
            self.assertIn(expected, f)

    def test_superficial_loss_events_handles_none_config(self):
        # The read seam degrades to an empty list on a None config
        # (defensive guard, DP#32 — absence yields nothing, never a crash).
        import model_fidelity
        self.assertEqual(model_fidelity.superficial_loss_events(None), [])

    def test_superficial_loss_events_handles_non_list_members(self):
        # A malformed config where family.members is not a list degrades
        # to an empty list rather than crashing (DP#32).
        import model_fidelity
        self.assertEqual(
            model_fidelity.superficial_loss_events(
                {'family': {'members': 'not-a-list'}}), [])


class TestRuleDefenseInDepth(unittest.TestCase):
    """The ``superficial_loss`` rule carries runtime guards that duplicate the
    contract loader's validations. The contract loader refuses bad events at
    load (``TestContractMapping`` above); these tests bypass the contract by
    building a legacy config directly — the same path the optimizer and
    ScenarioOverlay use — so the rule's own defense-in-depth guards fire.
    Driving ``FamilySimulation.run()`` exercises the rule in the fold, not a
    hand-built ``YearWorkingState`` (DP#11/DP#18)."""

    _START_YEAR = 2026

    @staticmethod
    def _legacy_cfg(members):
        return {
            'family': {'members': members, 'children': []},
            'accounts': {'rrsp_annual_max': 0},
            'assumptions': {'start_year': TestRuleDefenseInDepth._START_YEAR,
                            'horizon_age': 60,
                            'investment_return': 0.06, 'salary_growth': 0.0,
                            'inflation': 0.0, 'frozen_brackets': True},
            'portfolio': {
                'accounts': {
                    'non_reg': {
                        'balance': 0, 'cost_basis': 0,
                        'composition': {'cdn_equity_pct': 0.6,
                                        'fixed_income_pct': 0.4},
                        'yield': {'eligible_dividends': 0.02,
                                  'interest': 0.0},
                    },
                },
            },
            'property': {
                'house_value': 800_000, 'mortgage_balance': 0,
                'mortgage_rate': 0.05, 'margin_available': 0,
                'heloc_readvance': False, 'amortization_years': 25,
            },
            'household_budget': {'annual_living_costs': 60_000},
        }

    def test_non_member_acquirer_refuses_at_runtime(self):
        from simulation import FamilySimulation
        from simulation_config import SimulationConfig
        cfg = SimulationConfig.from_dict(self._legacy_cfg([
            {'role': 'primary', 'id': 'p1', 'birth_year': 1980,
             'gross_income': 150_000, 'retirement_age': 65,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'superficial_losses': [
                 {'year': self._START_YEAR, 'loss_amount': 100_000,
                  'acquired_by': 'ghost', 'days_to_acquisition': 19,
                  'still_held_30_days_after': True}]},
        ]))
        with self.assertRaises(ValueError):
            FamilySimulation(cfg).run()

    def test_non_positive_loss_refuses_at_runtime(self):
        from simulation import FamilySimulation
        from simulation_config import SimulationConfig
        cfg = SimulationConfig.from_dict(self._legacy_cfg([
            {'role': 'primary', 'id': 'p1', 'birth_year': 1980,
             'gross_income': 150_000, 'retirement_age': 65,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
             'superficial_losses': [
                 {'year': self._START_YEAR, 'loss_amount': 0,
                  'acquired_by': 'p2', 'days_to_acquisition': 19,
                  'still_held_30_days_after': True}]},
            {'role': 'spouse', 'id': 'p2', 'birth_year': 1982,
             'gross_income': 0, 'retirement_age': 65,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        ]))
        with self.assertRaises(ValueError):
            FamilySimulation(cfg).run()


if __name__ == '__main__':
    unittest.main()
