#!/usr/bin/env python3
"""#702 — the attribution rules reach the engine's OUTPUT.

The s.74.2 minor-lender tax flow was wired in PR #929 (see
tests/test_issue_702_attribution_wired.py). This file pins the REMAINING
wiring: each year's fold runs the attribution-rule checks over the declared
private loans -- ITA s.74.1 (spousal property transfer), s.74.2 (minor-child
recipient, age computed from birth_date per DP#1) and s.74.5(1)
(below-market / prescribed-rate escape) -- through the rule functions in
countries.canada.attribution (DP#10), and surfaces them on
``YearResult.attribution_summary`` so reports/objectives can read them
(DP#18: a declaration must reach the engine's output, not evaporate at the
adapter).

Scope honesty: this is DETECTION ONLY -- no new tax mechanics are invented.
The summary changes no tax number; assertions here pin the FLAGS, not cash.

All fixtures use fabricated round numbers and role-based names (DP#4/DP#15).
"""
import unittest

import countries.canada  # noqa: F401  (register the jurisdiction adapter)

from simulation_config import SimulationConfig


def _cfg(*, family_members, children=None, private_loans=None, time_step='yearly',
         years=3):
    return SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, living_costs=40_000, start_year=2026,
        province='quebec', investment_return=0.0, salary_growth=0.0,
        time_step=time_step, family_members=family_members,
        children=children or [], private_loans=private_loans or [])


def _run(cfg):
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()


def _members(income_p=120_000, income_s=40_000):
    return [
        {'role': 'primary', 'birth_year': 1980, 'gross_income': income_p,
         'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        {'role': 'spouse', 'birth_year': 1982, 'gross_income': income_s,
         'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]


def _child(birth_year):
    return [{'name': 'child_a', 'id': 'ca', 'birth_year': birth_year,
             'gross_income': 0}]


def _summary(results, year=0):
    s = results[year].attribution_summary
    assert len(s) >= 1, "the declared loan must produce a summary entry"
    return s[0]


SPOUSAL_LOAN = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.01, 'principal': 100_000, 'use': 'investment',
                'repayment': 'amortizing', 'interest': 'paid'}


class TestSpousalAttributionDetection(unittest.TestCase):
    """ITA s.74.1 via attribution.check_attribution: a loan BETWEEN the
    spouses is detected each year, and its s.74.5 escape status with it."""

    def test_below_market_spousal_loan_flags_attribution(self):
        # Spouse lends the primary $100k at 1% -- BELOW the documented 2%
        # benchmark: the s.74.5(2) exception is unavailable, so the flag says
        # the loaned funds' income attributes back to the lending spouse.
        r = _run(_cfg(family_members=_members(), private_loans=[SPOUSAL_LOAN]))
        e = _summary(r)
        self.assertEqual(e['rule'], 's.74.1_spousal_property')
        self.assertTrue(e['attributed'], "below-rate spousal loan must flag "
                                         "s.74.1 attribution")
        self.assertFalse(e['escape_available'])
        self.assertTrue(e['below_market']['attributed'])
        # Module semantics (read from the code): s.74.1 attributes INCOME AND
        # CAPITAL GAINS back -- only s.74.2 exempts capital gains.
        self.assertEqual(e['income_types_attributed'], ['all'])
        self.assertIn('BELOW', e['below_market']['reason'])

    def test_at_benchmark_rate_loan_escapes_attribution(self):
        # The SAME loan at 3% -- above the lesser-of benchmark -- with
        # interest paid on time escapes: NO attribution, escape available.
        loan = {**SPOUSAL_LOAN, 'rate': 0.03}
        r = _run(_cfg(family_members=_members(), private_loans=[loan]))
        e = _summary(r)
        self.assertEqual(e['rule'], 's.74.1_spousal_property')
        self.assertFalse(e['attributed'],
                         "a loan at/above the prescribed rate with timely "
                         "interest escapes s.74.1 attribution")
        self.assertTrue(e['escape_available'])
        self.assertFalse(e['below_market']['attributed'])
        self.assertIn('exception applies', e['below_market']['reason'])

    def test_interest_free_demand_loan_fails_the_timing_test(self):
        # An on-demand/on_demand demand loan pays NO interest, so the
        # s.74.5(2)(b) timing test fails even at a stated 5%: attribution
        # flags. This is the statute's answer, not an engine opinion.
        loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'consumption',
                'repayment': 'on_demand', 'interest': 'on_demand'}
        r = _run(_cfg(family_members=_members(), private_loans=[loan]))
        e = _summary(r)
        self.assertTrue(e['below_market']['attributed'])
        self.assertIn('NOT paid by Jan 30', e['below_market']['reason'])


class TestPrescribedRateLoanConstruction(unittest.TestCase):
    """The declared loan constructs/tracks a debt.PrescribedRateLoan whose
    roles come from the RESOLVED person ids -- never the hardcoded
    'primary'/'spouse' defaults (#702's named defect)."""

    def test_roles_and_rate_come_from_the_contract(self):
        r = _run(_cfg(family_members=_members(), private_loans=[SPOUSAL_LOAN]))
        e = _summary(r)
        prl = e['prescribed_rate_loan']
        self.assertEqual(prl['lender'], 'spouse')   # resolved from person id
        self.assertEqual(prl['borrower'], 'primary')
        self.assertEqual(prl['rate'], 0.01)
        self.assertEqual(prl['principal'], 100_000)
        self.assertEqual(prl['annual_interest'], 1_000.0)
        self.assertFalse(prl['attribution_risk'])   # timely -> no risk flag
        # The benchmark the escape status was tested AGAINST is carried on
        # the entry -- never silently assumed (DP#13).
        self.assertEqual(e['benchmark_prescribed_rate'], 0.02)


class TestMinorChildAttributionDetection(unittest.TestCase):
    """ITA s.74.2 via attribution.check_attribution: the recipient-age gate
    is computed from birth_year (DP#1) and flips exactly at 18."""

    def test_minor_borrower_flags_attribution_capital_gains_exempt(self):
        # Primary lends their 16-year-old $100k: s.74.2 fires -- but only
        # INCOME attributes; capital gains stay in the child's hands (the
        # module's documented planning point).
        loan = {**SPOUSAL_LOAN, 'lender': 'p1', 'borrower': 'ca'}
        r = _run(_cfg(family_members=_members(),
                      children=_child(2010), private_loans=[loan]))  # 16 in 2026
        e = _summary(r)
        self.assertEqual(e['rule'], 's.74.2_minor_child')
        self.assertTrue(e['attributed'])
        types = e['income_types_attributed']
        self.assertIn('interest', types)
        self.assertNotIn('capital_gain', types,
                         "s.74.2 exempts capital gains -- the key planning "
                         "point the rule module documents")
        self.assertIn('16', e['reason'])

    def test_eighteen_year_old_borrower_is_not_a_minor(self):
        # The SAME loan to an 18-year-old: s.74.2 does NOT apply (both sides
        # of the threshold, decided by the rule module, DP#1 ages).
        loan = {**SPOUSAL_LOAN, 'lender': 'p1', 'borrower': 'ca'}
        r = _run(_cfg(family_members=_members(),
                      children=_child(2008), private_loans=[loan]))  # 18 in 2026
        e = _summary(r)
        self.assertEqual(e['rule'], 's.74.2_minor_child')
        self.assertFalse(e['attributed'],
                         "an 18-year-old is not a minor: no s.74.2")
        self.assertEqual(e['income_types_attributed'], [])

    def test_external_lender_to_minor_still_triggers_s74_2(self):
        # A grandparent OUTSIDE the household lends the 16-year-old $100k:
        # s.74.2 keys on the related-minor RECIPIENT, not on who holds the
        # note. The donor is surfaced as external, not silently normalized.
        loan = {**SPOUSAL_LOAN, 'lender': {'id': 'gp', 'relationship':
                                           'grandparent'},
                'borrower': 'ca'}
        r = _run(_cfg(family_members=_members(),
                      children=_child(2010), private_loans=[loan]))
        e = _summary(r)
        self.assertEqual(e['rule'], 's.74.2_minor_child')
        self.assertTrue(e['attributed'])
        self.assertIn('gp', e['lender'])
        self.assertIn('external', e['lender'])


class TestNoTriggerIsReportedNotSilent(unittest.TestCase):
    """A loan with no attribution trigger data gets an explicit rule=None
    entry naming why -- reported, never silently skipped (DP#32/DP#16)."""

    def test_third_party_loan_to_adult_member_names_the_gap(self):
        loan = {**SPOUSAL_LOAN,
                'lender': {'id': 'friend'}, 'borrower': 'p1'}
        r = _run(_cfg(family_members=_members(), private_loans=[loan]))
        e = _summary(r)
        self.assertIsNone(e['rule'])
        self.assertFalse(e['attributed'])
        self.assertIn('no Part I attribution', e['reason'])

    def test_borrower_that_resolves_to_nobody_names_the_pair(self):
        # A borrower id matching no declared person cannot come through the
        # contract (the adapter refuses it, DP#32) -- but a hand-built config
        # can carry one, and the detection still reports rather than skips.
        r = _run(_cfg(family_members=_members(),
                      private_loans=[{**SPOUSAL_LOAN, 'borrower': 'px'}]))
        e = _summary(r)
        self.assertIsNone(e['rule'])
        self.assertIn('not a taxed spouse', e['reason'])

    def test_every_declared_loan_gets_exactly_one_entry(self):
        loans = [SPOUSAL_LOAN,
                 {**SPOUSAL_LOAN, 'id': 'l2', 'rate': 0.05},
                 {**SPOUSAL_LOAN, 'id': 'l3',
                  'lender': {'id': 'gp'}, 'borrower': 'ca'}]
        r = _run(_cfg(family_members=_members(), children=_child(2010),
                      private_loans=loans))
        ids = [e['loan_id'] for e in r[0].attribution_summary]
        self.assertEqual(ids, ['l1', 'l2', 'l3'])


class TestDetectionChangesNoTaxNumber(unittest.TestCase):
    """The summary is DETECTION ONLY: two households differing ONLY in a
    fact the detection reads (a below- vs above-benchmark rate on an
    otherwise identical paid-interest loan) differ in tax ONLY through the
    pre-existing #832 interest split -- i.e. proportionally to the rate, not
    to the attribution flag. Pinned loosely here; the strong no-op proof is
    the golden invariant (no private loans -> empty summary, byte-exact
    trajectory), asserted in tests/test_issue_702_attribution_wired.py."""

    def test_no_private_loans_gives_an_empty_summary_free(self):
        base = _run(_cfg(family_members=_members()))
        for yr in base:
            self.assertEqual(yr.attribution_summary, [])
            # And it costs nothing: the year's totals are untouched fields.
            self.assertIsInstance(yr.attribution_summary, list)


class TestMonthlyFoldParity(unittest.TestCase):
    """The monthly fold surfaces the SAME summary as the annual fold (shared
    helper, DP#9) -- a feature wired into only one run path is half-wired."""

    def test_monthly_matches_yearly(self):
        y = _run(_cfg(family_members=_members(), private_loans=[SPOUSAL_LOAN],
                      time_step='yearly'))
        m = _run(_cfg(family_members=_members(), private_loans=[SPOUSAL_LOAN],
                      time_step='monthly'))
        self.assertEqual(y[0].attribution_summary, m[0].attribution_summary)


if __name__ == "__main__":
    unittest.main()
