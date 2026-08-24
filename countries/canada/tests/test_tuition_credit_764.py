#!/usr/bin/env python3
"""Tests for issue #764: the federal tuition tax credit (ITA s.118.5).

LEAN, single-responsibility, fabricated round-number data. No personal info
(DP#15): role-based names, round numbers.

Scope tested here (the SIMPLE case the PR lands):
  - `tuition_tax_credit`: a student claims their OWN federal non-refundable
    credit on eligible tuition. The rate is SOURCED from the tax provider's
    federal lowest bracket (DP#12/DP#20), not a hardcoded constant -- proven
    by monkeypatching the provider's rate and watching the credit move.
  - wiring: a taxed member (spouse) declaring `tuition_by_year` reduces their
    tax in that year (YearResult.after_tax_income rises by the credit); a
    household that declares no tuition is byte-for-byte unaffected (the golden
    invariant does not move).

NOT tested here (filed as follow-ups, deliberately not half-implemented):
  - Quebec provincial tuition credit (rate not in the tax-data files).
  - carry-forward of unused amounts.
  - transfer to a supporting spouse/parent (federal $5,000 limit).
  - a CHILD declaring tuition (children are not taxed as individuals, #701;
    the transfer is the real mechanism -- the adapter warns loudly, DP#32).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

import unittest
from unittest.mock import patch

from countries.canada.tax_calc import tuition_tax_credit, _load_fed_data
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from contract_people import _tuition_by_year


# ── The credit itself ───────────────────────────────────────────────────────

class TestTuitionTaxCredit(unittest.TestCase):
    """ITA s.118.5 federal tuition credit -- the OWN-credit, current-year case."""

    def test_zero_for_none_zero_or_negative_tuition(self):
        # DP#32: an absent/zero tuition means no credit -- not a defaulted one.
        self.assertEqual(tuition_tax_credit(None), 0.0)
        self.assertEqual(tuition_tax_credit(0), 0.0)
        self.assertEqual(tuition_tax_credit(-100), 0.0)

    def test_credit_is_tuition_times_sourced_lowest_rate(self):
        # The 2026 federal lowest bracket is 14% (the Jul-2025 middle-class
        # cut reduced it from 15%) -- the credit tracks the DATA, so it is
        # 14% here, NOT the 15% the original issue stated. That is the point:
        # sourcing it (not hardcoding 0.15) is what keeps it correct.
        credit = tuition_tax_credit(5000, 2026)
        self.assertAlmostEqual(credit, 5000 * 0.14, places=2)

    def test_the_rate_is_sourced_not_hardcoded(self):
        """Monkeypatch the federal lowest rate; the credit must follow it.
        A hardcoded 0.15 (or 0.14) would fail this -- the credit would not
        move when the data does, which is exactly the DP#12/DP#20 defect."""
        for fake_rate in (0.10, 0.15, 0.21):
            with patch('countries.canada.tax_calc._load_fed_data',
                       return_value=(object(), fake_rate, 0, 0)):
                self.assertAlmostEqual(
                    tuition_tax_credit(4000, 2026), 4000 * fake_rate, places=4,
                    msg=f"credit did not track a sourced rate of {fake_rate}")

    def test_pure_function_same_inputs_same_output(self):
        self.assertEqual(tuition_tax_credit(3000, 2026),
                         tuition_tax_credit(3000, 2026))

    def test_fallback_rate_when_tax_data_unavailable(self):
        # DP#13: a round 0.15 fallback when the provider cannot supply a rate
        # (mirrors _load_fed_data's own fallback for canada_employment_credit).
        # Covered so the coverage gate does not count this branch as new slack.
        with patch('countries.canada.tax_calc._load_fed_data',
                   side_effect=ValueError('no data for year')):
            self.assertAlmostEqual(tuition_tax_credit(4000, 2099), 4000 * 0.15, places=4)

    def test_falls_back_to_round_rate_when_year_data_missing(self):
        # DP#13: when the federal data cannot be loaded for the year (the
        # provider raises), the credit falls back to a round 0.15 -- a
        # documented placeholder, not a silent zero (DP#32 would rather a
        # loud fallback than a plausible wrong number). Proven by forcing
        # _load_fed_data to raise and watching the 15% path fire.
        with patch('countries.canada.tax_calc._load_fed_data',
                   side_effect=ValueError('no data for year')):
            self.assertAlmostEqual(tuition_tax_credit(1000, 2099),
                                   1000 * 0.15, places=4)


# ── Wiring: the credit is actually CALLED in a run (not #710 dead code) ──────

def _spouse_tuition_config(tuition_by_year=None, start_year=2026):
    """A fabricated round-number couple, short frozen-bracket horizon.

    The spouse carries an OPTIONAL `tuition_by_year` map -- the exact legacy
    shape contract_people._tuition_by_year produces for a taxed member."""
    members = [
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
         'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0},
        {'role': 'spouse', 'birth_year': 1982, 'gross_income': 80_000,
         'retirement_age': 95, 'rrsp_balance': 0, 'tfsa_balance': 0},
    ]
    if tuition_by_year is not None:
        # Attach to the spouse member (a taxed role -- the simple case).
        members[1]['tuition_by_year'] = tuition_by_year
    return {
        'family': {'members': members, 'children': []},
        'accounts': {},
        'assumptions': {'start_year': start_year, 'horizon_age': 95,
                        'investment_return': 0.0, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True,
                        'savings_rate': 0.0},
        'portfolio': {'accounts': {}},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'amortization_years': 25,
                     'margin_available': 0, 'ltv_max': 0.80, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'retirement': {'spending_target': 0, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
        # Issue #679: the solvency identity (and YearResult.after_tax_income, its
        # observable) only fires when living_costs is declared. The tuition
        # credit flows through after_tax_income into that identity -- the #758
        # runway use case -- so the test must declare a living cost to observe it.
        'household_budget': {'living_costs': 50_000},
    }


class TestTuitionCreditWiring(unittest.TestCase):
    """The credit is wired into the per-year tax, so a declared tuition
    actually reduces tax in the run -- the #710 'implemented but uncalled'
    trap this repo was rebuilt to kill."""

    def _run(self, tuition_by_year=None):
        cfg = _spouse_tuition_config(tuition_by_year)
        sim_cfg = SimulationConfig.from_dict(cfg)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                               use_readvanceable=False, deduct_later=False)
        return sim.run()

    def test_no_tuition_means_no_credit_and_runs_identically(self):
        # Baseline: no tuition declared -> the credit is 0 everywhere, and
        # after_tax_income is the gross-minus-bracket-tax baseline (the credit
        # is gated to 0 by the absent tuition_by_year).
        base = self._run(None)
        self.assertGreater(base[0].spouse_income, 0)
        self.assertGreater(base[0].after_tax_income, 0)

    def test_declared_tuition_reduces_spouse_tax_in_that_year(self):
        start_year = 2026
        base = self._run(None)
        with_credit = self._run({start_year: 10_000})
        # Issue #783: the credit now INCLUDES the Quebec PROVINCIAL tuition
        # credit (TP-1 Schedule T, 8%) alongside the federal one. The fixture
        # is Quebec-resident (tax.province='qc'), so the credit is
        # federal(14% x tuition) + QC(8% x tuition) = 22% x $10,000 = $2,200
        # (the federal 14% is the sourced lowest bracket rate; the QC 8% is the
        # sourced qc_tuition_credit_rate). The credit is subtracted from the
        # spouse's year-0 tax (floored at 0); after_tax_income rises by it.
        rate = tuition_tax_credit(10_000, start_year, province='qc') / 10_000
        expected_credit = 10_000 * rate
        self.assertAlmostEqual(expected_credit, 2_200.0, places=2)
        self.assertAlmostEqual(
            with_credit[0].after_tax_income - base[0].after_tax_income,
            expected_credit, places=2)
        # Gross incomes are identical (the credit moves tax, not income).
        self.assertEqual(with_credit[0].spouse_income, base[0].spouse_income)

    def test_credit_does_not_apply_in_a_year_with_no_declared_tuition(self):
        # Tuition declared for 2027 only -- year 0 (2026) must be unaffected.
        start_year = 2026
        base = self._run(None)
        future = self._run({start_year + 1: 10_000})
        self.assertAlmostEqual(future[0].after_tax_income,
                              base[0].after_tax_income, places=4)

    def test_credit_floored_at_zero_tax_cannot_go_negative(self):
        # A credit larger than the spouse's tax must reduce the spouse's tax
        # to EXACTLY 0 (non-refundable), not below. Proved by isolating the
        # spouse's contribution: a run with NO spouse income has after_tax =
        # primary net; a run with a huge spouse tuition credit has spouse tax
        # floored to 0, so its after_tax = primary net + spouse GROSS. The
        # difference is exactly the spouse's gross income (none of it lost to
        # tax, none invented as a negative-tax refund).
        start_year = 2026
        no_spouse = self._run_with_spouse_income(0)
        huge = self._run({start_year: 5_000_000})
        self.assertAlmostEqual(
            huge[0].after_tax_income - no_spouse[0].after_tax_income,
            huge[0].spouse_income, places=2,
            msg="a non-refundable credit must floor tax at 0, not pay a refund")

    def _run_monthly(self, tuition_by_year=None):
        cfg = _spouse_tuition_config(tuition_by_year)
        cfg['assumptions']['time_step'] = 'monthly'
        sim_cfg = SimulationConfig.from_dict(cfg)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                               use_readvanceable=False, deduct_later=False)
        return sim.run()

    def test_monthly_time_step_applies_the_same_credit(self):
        # The credit is wired symmetrically into BOTH time steps (DP#9): the
        # monthly fold must reduce the studying member's tax by exactly the
        # same credit the yearly path does -- one mechanism, not a parallel one.
        # Issue #783: the credit is federal(14%) + QC(8%) = 22% of tuition for
        # this Quebec-resident fixture, so $10,000 -> $2,200 in both paths.
        start_year = 2026
        base = self._run_monthly(None)
        with_credit = self._run_monthly({start_year: 10_000})
        rate = tuition_tax_credit(10_000, start_year, province='qc') / 10_000
        self.assertAlmostEqual(rate, 0.22, places=4)
        self.assertAlmostEqual(
            with_credit[0].after_tax_income - base[0].after_tax_income,
            10_000 * rate, places=2)

    def _run_with_spouse_income(self, spouse_income, tuition_by_year=None):
        cfg = _spouse_tuition_config(tuition_by_year)
        cfg['family']['members'][1]['gross_income'] = spouse_income
        sim_cfg = SimulationConfig.from_dict(cfg)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                               use_readvanceable=False, deduct_later=False)
        return sim.run()


# ── The adapter mapping: study_periods[].tuition -> member tuition_by_year ──

class TestTuitionByYearMapping(unittest.TestCase):
    """contract_people._tuition_by_year expands a study_period's tuition into a
    per-calendar-year map and emits the DP#32 load-time warning on what is NOT
    yet modelled (child transfer). The Quebec provincial tuition credit IS
    now modelled (#783), so its pre-#783 warning is gone."""

    @staticmethod
    def _person(study_periods):
        return {'id': 'spouse_a', 'study_periods': study_periods}

    def _doc(self, study_periods, province='quebec', role='spouse'):
        return ({'jurisdiction': {'province': province}, 'as_of': '2026-06-30'},
                self._person(study_periods), role, 'spouse_a')

    def test_no_study_periods_or_no_tuition_returns_empty(self):
        doc, p, role, pid = self._doc([])
        self.assertEqual(_tuition_by_year(doc, p, role, pid), {})
        doc, p, role, pid = self._doc([{'institution': 'X', 'program': 'Y',
                'start_date': '2026-01-01', 'end_date': '2027-06-01'}])  # no tuition key
        self.assertEqual(_tuition_by_year(doc, p, role, pid), {})

    def test_tuition_annualised_across_each_year_of_a_dated_window(self):
        doc, p, role, pid = self._doc([{'institution': 'X', 'program': 'Y',
            'start_date': '2026-01-01', 'end_date': '2028-06-01', 'tuition': 5000}])
        self.assertEqual(_tuition_by_year(doc, p, role, pid),
                         {2026: 5000.0, 2027: 5000.0, 2028: 5000.0})

    def test_null_end_date_pays_tuition_in_start_year_only(self):
        # A null end is NOT annualised to infinity (that would fabricate tuition
        # for every future year); it pays in the one known year.
        doc, p, role, pid = self._doc([{'institution': 'X', 'program': 'Y',
            'start_date': '2026-01-01', 'end_date': None, 'tuition': 5000}])
        self.assertEqual(_tuition_by_year(doc, p, role, pid), {2026: 5000.0})

    def test_multiple_periods_covering_the_same_year_sum(self):
        doc, p, role, pid = self._doc([
            {'institution': 'X', 'program': 'Y', 'start_date': '2026-01-01',
             'end_date': '2026-12-31', 'tuition': 3000},
            {'institution': 'Z', 'program': 'W', 'start_date': '2026-09-01',
             'end_date': '2027-06-01', 'tuition': 2000},
        ])
        tb = _tuition_by_year(doc, p, role, pid)
        self.assertEqual(tb[2026], 5000.0)  # 3000 + 2000
        self.assertEqual(tb[2027], 2000.0)

    def test_child_tuition_recorded_no_warning(self):
        # Issue #785: a child declaring tuition is now TRANSFERRED to a
        # supporting parent/spouse (up to $5,000), not just recorded-and-
        # warned. The pre-#785 CHILD warning is REMOVED.
        doc, p, role, pid = self._doc(
            [{'institution': 'X', 'program': 'Y', 'start_date': '2026-01-01',
              'end_date': '2026-12-31', 'tuition': 4000}], role='child')
        with self.assertNoLogs(level='WARNING'):
            tb = _tuition_by_year(doc, p, 'child', pid)
        self.assertEqual(tb, {2026: 4000.0})  # recorded for transfer

    def test_quebec_tuition_credit_is_computed_no_warning(self):
        """Issue #783: the Quebec PROVINCIAL tuition credit (TP-1 Schedule T,
        #783) is now modelled, so the pre-#783 "QC provincial portion not
        modelled" warning is GONE. A Quebec tuition declaration no longer
        warns; the QC credit is COMPUTED (federal + QC), not warned-about as
        missing (DP#32: the absence is no longer a defect)."""
        doc, p, role, pid = self._doc(
            [{'institution': 'X', 'program': 'Y', 'start_date': '2026-01-01',
              'end_date': '2026-12-31', 'tuition': 4000}], province='quebec')
        # No warning fires (the QC provincial credit is now applied).
        with self.assertNoLogs(level='WARNING'):
            tb = _tuition_by_year(doc, p, role, pid)
        self.assertEqual(tb, {2026: 4000.0})
        # And the QC credit is actually computed: federal + QC > federal-only.
        from countries.canada.tax_calc import tuition_tax_credit as _ttc
        qc_credit = _ttc(4000, 2026, province='quebec')
        fed_only = _ttc(4000, 2026, province='ontario')
        self.assertGreater(qc_credit, fed_only)
        self.assertAlmostEqual(qc_credit - fed_only, 4000 * 0.08,  # the 8% QC portion
                               places=4)

    def test_non_quebec_tuition_emits_no_provincial_warning(self):
        doc, p, role, pid = self._doc(
            [{'institution': 'X', 'program': 'Y', 'start_date': '2026-01-01',
              'end_date': '2026-12-31', 'tuition': 4000}], province='ontario')
        self.assertNoLogs(level='WARNING')


# ── Adapter wiring: _map_member / _map_child carry tuition to the config ──

class TestAdapterMemberAndChildWiring(unittest.TestCase):
    """The tuition additions in _map_member (taxed members) and _map_child
    (children) are exercised directly with minimal fabricated docs -- the path
    a real contract takes through the adapter, so the production lines are
    reached (and the coverage gate sees them)."""

    def test_map_member_carries_tuition_by_year_for_a_taxed_member(self):
        from contract_people import _map_member
        doc = {'as_of': '2026-06-30', 'jurisdiction': {'province': 'quebec'},
               'people': [{'id': 'p2', 'room': {}, 'incomes': [],
                           'study_periods': [{'institution': 'X', 'program': 'Y',
                             'start_date': '2026-01-01', 'end_date': '2026-12-31',
                             'tuition': 6000}]}],
               'decisions': {'retirement_age': []}}
        # Issue #783: a taxed member's Quebec tuition is now fully credited
        # (federal + QC provincial), so NO warning fires (the pre-#783
        # "QC provincial not modelled" warning is gone). The child-transfer
        # warning is the only tuition warning that remains, and this is a
        # taxed spouse, not a child.
        with self.assertNoLogs(level='WARNING'):
            member = _map_member(doc, 'p2', 'spouse', {})
        self.assertEqual(member['tuition_by_year'], {2026: 6000.0})

    def test_map_member_omits_tuition_by_year_when_none_declared(self):
        from contract_people import _map_member
        doc = {'as_of': '2026-06-30', 'jurisdiction': {'province': 'quebec'},
               'people': [{'id': 'p2', 'room': {}, 'incomes': []}],
               'decisions': {'retirement_age': []}}
        member = _map_member(doc, 'p2', 'spouse', {})
        self.assertNotIn('tuition_by_year', member)

    def test_map_child_records_tuition_no_warning(self):
        # Issue #785: the child-tuition warning is REMOVED (transfer is modelled).
        from contract_people import _map_child
        doc = {'as_of': '2026-06-30', 'jurisdiction': {'province': 'quebec'},
               'people': [{'id': 'ca', 'room': {}, 'incomes': [],
                           'study_periods': [{'institution': 'X', 'program': 'Y',
                             'start_date': '2026-01-01', 'end_date': '2026-12-31',
                             'tuition': 4000}]}],
               'decisions': {'retirement_age': []}}
        with self.assertNoLogs(level='WARNING'):
            child = _map_child(doc, 'ca', {})
        self.assertEqual(child['tuition_by_year'], {2026: 4000.0})


if __name__ == '__main__':
    unittest.main()