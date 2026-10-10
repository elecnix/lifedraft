"""Issue #365: the Quebec career-extension credit (TP-1 line 391).

A non-refundable credit on the WORK income of an experienced worker who keeps
working, or returns to work, past retirement age. Its absence overstated the
tax on that income and so understated the payoff of working longer -- every
``retirement_age`` candidate was priced without it.

Every figure below is re-cited from the primary sources rather than copied from
the issue (#273):

  * Revenu Quebec, Line 391 -- "Credit d'impot pour prolongation de carriere":
    age 65 or over at December 31 2025, maximum credit 1,750.
  * Revenu Quebec, form TP-752.PC: for taxation years BEFORE 2025 the age floor
    was 60, not 65.
  * Ministere des Finances du Quebec, *Depenses fiscales -- Edition 2024*
    (mars 2025), p. C.131 -- the parameters table, which also records the
    November 2024 changes: age floor 60 -> 65, exclusion 5,000 -> 7,500, maximum
    eligible work income 11,000 -> 12,500, reduction threshold 40,925 -> 56,500,
    reduction rate 5% -> 7%, and the reduction BASE moving from work income to
    individual NET income (line 275).
  * Loi sur les impots, art. 752.0.10.0.2 and 752.0.10.0.3.

The 2026 amounts are those of 2025 indexed: 12,755 / 7,655 / 57,660.

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.provinces.quebec.quebec_credits import (
    quebec_career_extension_credit,
    quebec_non_refundable_credits,
)


class TestTheAcceptanceCases:
    """Cases (a)-(g) of the issue, re-derived from the primary sources above."""

    @pytest.mark.parametrize('label, year, age, work, net, expected', [
        # (a) 2024, 67: 14% of (10,000 - 5,000) = 700.
        ('a', 2024, 67, 10_000, 10_000, 700.00),
        # (b) 2024, 67, 50,000: capped at 11,000 -> 1,540, less 5% of the work
        # income above 40,925 (2024 reduces on WORK income, not net income).
        ('b', 2024, 67, 50_000, 50_000, 1_086.25),
        # (c) 2024, 62: the 60-64 cap is 10,000 -> 1,400.
        ('c', 2024, 62, 20_000, 20_000, 1_400.00),
        # (d) 2025, 62: the floor is now 65, so nothing.
        ('d', 2025, 62, 20_000, 20_000, 0.00),
        # (e) 2025, 66: 14% of the 12,500 cap = 1,750.
        ('e', 2025, 66, 30_000, 30_000, 1_750.00),
        # (f) 2025, 66, net 70,000: less 7% of the NET income above 56,500.
        ('f', 2025, 66, 30_000, 70_000, 805.00),
        # (g) 2025, net 81,500: fully reduced away.
        ('g', 2025, 66, 30_000, 81_500, 0.00),
    ])
    def test_case(self, label, year, age, work, net, expected):
        got = quebec_career_extension_credit(
            eligible_work_income=work, net_income=net, age=age, year=year)
        assert got == pytest.approx(expected, abs=0.005), (
            f"case ({label}): {year} age {age}, work {work:,}, net {net:,} "
            f"-> {got:,.2f}, expected {expected:,.2f}")

    def test_2026_is_the_indexed_2025(self):
        """The 2026 amounts are 2025 indexed (12,755 / 7,655 / 57,660)."""
        assert quebec_career_extension_credit(
            eligible_work_income=30_000, net_income=30_000, age=66,
            year=2026) == pytest.approx(12_755 * 0.14, abs=0.005)


class TestTheCreditsBoundaries:
    """The thresholds behave as thresholds -- both sides of each."""

    def test_work_income_at_the_exclusion_earns_nothing(self):
        """The Ministere states it plainly: no credit at or below 7,500."""
        assert quebec_career_extension_credit(
            eligible_work_income=7_500, net_income=7_500, age=66,
            year=2025) == 0.0

    def test_one_dollar_above_the_exclusion_earns_something(self):
        """The other side of the same line: the credit is not a cliff."""
        got = quebec_career_extension_credit(
            eligible_work_income=7_501, net_income=7_501, age=66, year=2025)
        assert got == pytest.approx(0.14, abs=0.005)

    def test_the_maximum_is_reached_at_exclusion_plus_cap(self):
        """The Ministere's own illustration: the cap is reached at 20,000 of
        work income in 2025, which is the 7,500 exclusion PLUS the 12,500 cap.
        This pins the order of operations -- excluding after capping would put
        the ceiling at 12,500 of work income instead, understating every claim
        in between."""
        at_ceiling = quebec_career_extension_credit(
            eligible_work_income=20_000, net_income=20_000, age=66, year=2025)
        above = quebec_career_extension_credit(
            eligible_work_income=90_000, net_income=20_000, age=66, year=2025)
        assert at_ceiling == pytest.approx(1_750.00, abs=0.005)
        assert above == pytest.approx(1_750.00, abs=0.005)

    def test_the_age_floor_is_a_floor_at_the_turn_of_2025(self):
        """64 gets nothing in 2025, 65 gets the credit -- the reform at work."""
        assert quebec_career_extension_credit(
            eligible_work_income=30_000, net_income=30_000, age=64,
            year=2025) == 0.0
        assert quebec_career_extension_credit(
            eligible_work_income=30_000, net_income=30_000, age=65,
            year=2025) > 0.0
        # ... and 64 DID get it in 2024, when the floor was still 60.
        assert quebec_career_extension_credit(
            eligible_work_income=30_000, net_income=30_000, age=64,
            year=2024) > 0.0

    def test_no_work_income_earns_nothing_at_any_age(self):
        """The credit is ON WORK. A 70-year-old with only pension income is
        not eligible, and passing 0 must never produce a credit."""
        for age in (60, 65, 70, 85):
            assert quebec_career_extension_credit(
                eligible_work_income=0, net_income=120_000, age=age,
                year=2025) == 0.0

    def test_an_undateable_age_is_declined_not_guessed(self):
        """DP#32: a missing age must not be read as an eligible one."""
        for bad in (None, '66', True, -1):
            assert quebec_career_extension_credit(
                eligible_work_income=30_000, net_income=30_000, age=bad,
                year=2025) == 0.0


class TestTheAggregatorCarriesIt:
    """The credit must reach the aggregate the fold consumes, or it is a sixth
    dead credit -- the shape this repo has been burned by."""

    def test_the_aggregate_reports_the_career_credit(self):
        out = quebec_non_refundable_credits(
            net_income=30_000, year=2025, eligible_work_income=30_000, age=66)
        assert out['career_extension_credit'] == pytest.approx(1_750.0, abs=0.005)
        assert out['total_credit'] >= out['career_extension_credit']

    def test_an_absent_age_leaves_the_credit_at_zero(self):
        """The absence-safe default: a caller that supplies no age claims no
        career credit rather than inventing a 0-year-old claimant."""
        out = quebec_non_refundable_credits(
            net_income=30_000, year=2025, eligible_work_income=30_000)
        assert out['career_extension_credit'] == 0.0


class TestTheFoldChargesItPerMember:
    """Case (h): through the fold's own per-adult tax, not a reimplementation."""

    @staticmethod
    def _cfg(province='quebec', gross=20_000, birth_year=1959):
        return {
            'family': {'members': [{'role': 'primary',
                                    'birth_year': birth_year,
                                    'retirement_age': 70,
                                    'gross_income': gross,
                                    'rrsp_room_accumulated': 0,
                                    'tfsa_room_accumulated': 0}],
                       'children': []},
            'assumptions': {'start_year': 2025, 'projection_years': 3,
                            'investment_return': 0.05, 'salary_growth': 0.0,
                            'inflation': 0.0, 'frozen_brackets': True},
            'tax': {'province': province},
        }

    def _tax(self, cfg_dict, work):
        from simulation_config import SimulationConfig
        from simulation import _income_tax_by_adult
        from tax_data import default_tax_provider
        cfg = SimulationConfig.from_dict(cfg_dict)
        brackets = default_tax_provider().get_combined_brackets(2025)
        return _income_tax_by_adult(
            cfg, {'primary': work, 'spouse': 0.0},
            {'primary': (0.0, 0.0), 'spouse': (0.0, 0.0)}, brackets,
            work_by_role={'primary': work, 'spouse': 0.0}, calendar_year=2025)

    def test_a_66_year_old_quebecer_pays_1750_less(self):
        """The primary is 66 in 2025 and earns 20,000: the credit is 1,750 and
        the tax charged falls by exactly that."""
        with_credit = self._tax(self._cfg(), 20_000)
        p = with_credit['primary']
        assert p['career_extension_credit'] == pytest.approx(1_750.0, abs=0.5)
        assert p['tax_after_credits'] == pytest.approx(
            p['tax_before'] - p['career_extension_credit'])

    def test_an_ontario_household_is_untouched(self):
        """The credit is Quebec's alone; Ontario must not see a cent of it."""
        p = self._tax(self._cfg(province='ontario'), 20_000)['primary']
        assert p['career_extension_credit'] == 0.0
        assert p['tax_after_credits'] == pytest.approx(p['tax_before'])

    def test_the_credit_is_capped_at_the_tax_otherwise_payable(self):
        """Non-refundable means non-refundable: on tiny work income the credit
        cannot exceed the tax, so it never becomes a payment."""
        p = self._tax(self._cfg(gross=7_600), 7_600)['primary']
        assert p['tax_after_credits'] >= 0.0, (
            "a non-refundable credit must never drive tax below zero")
        assert p['tax_after_credits'] == pytest.approx(
            max(0.0, p['tax_before'] - p['career_extension_credit']))

    def test_a_retired_member_claims_nothing(self):
        """The fold zeroes a retired member's income before this tax step, so
        a pensioner's work income is 0 and the credit is 0 -- which is what
        keeps the golden household (both retired at 65) byte-identical."""
        p = self._tax(self._cfg(birth_year=1950), 0.0)['primary']
        assert p['career_extension_credit'] == 0.0


class TestTheGoldenInvariant:
    def test_the_golden_trajectory_is_byte_identical(self):
        """The credit cannot fire on the golden household: it is Quebec, both
        adults retire at 65, and the fold zeroes their income at 65 -- so the
        age gate and the work-income gate can never both pass. Asserted rather
        than hoped."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tests'))
        from test_golden_trajectory_581 import (golden_household_config,
                                                _run as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)