#!/usr/bin/env python3
"""Issue #390: always-on CPP/QPP estimate from incomes + salary_growth.

When no Service Canada / Retraite Québec Statement is present, the contract
path derives an age-65 monthly estimate from earnings_history and/or
employment incomes, extending future years with assumptions.salary_growth
instead of zero-padding after the last history year. A Statement always
wins. Quebec residency selects QPP max-benefit tables (YMPE/CPP2 remain
CPP-table-centric — documented).

All test data is synthetic (DP#15).
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from countries.canada.cpp_estimator import (
    EarningsEntry,
    build_earnings_for_estimate,
    compute_benefit_estimate,
)
from countries.canada.retirement_transition import member_retirement_income
from test_input_contract import _load_example, _two_generation_subset


def _strip_statement(person: dict) -> None:
    person.pop("entitlements", None)
    benefits = person.get("benefits") or {}
    benefits.pop("cpp", None)
    if benefits:
        person["benefits"] = benefits
    else:
        person.pop("benefits", None)


def _primary(doc):
    legacy = ic.to_internal_config(doc)
    return next(m for m in legacy["family"]["members"] if m["role"] == "primary"), legacy


class BuildEarningsSeries(unittest.TestCase):
    """Pure helper: history + incomes + salary_growth overlay."""

    def test_extends_past_last_history_with_growth(self):
        history = [{"year": y, "employment_income": 80_000} for y in range(2000, 2021)]
        entries = build_earnings_for_estimate(
            earnings_history=history,
            incomes=[{
                "kind": "employment", "amount": 90_000,
                "from": "2015-01-01", "to": None,
            }],
            salary_growth=0.02,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        years = [e.year for e in entries]
        self.assertEqual(min(years), 2000)
        # birth 1980 + 65 - 1 = 2044
        self.assertEqual(max(years), 2044)
        # History years unchanged.
        self.assertEqual(
            next(e.employment_income for e in entries if e.year == 2010),
            80_000,
        )
        # Future year grown from as_of active income (90k @ 2%).
        y2030 = next(e.employment_income for e in entries if e.year == 2030)
        self.assertAlmostEqual(y2030, 90_000 * (1.02 ** 4), places=2)

    def test_incomes_only_fills_from_interval_then_grows(self):
        entries = build_earnings_for_estimate(
            earnings_history=None,
            incomes=[{
                "kind": "employment", "amount": 100_000,
                "from": "2015-01-01", "to": None,
            }],
            salary_growth=0.03,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2015], 100_000)
        self.assertEqual(by_year[2026], 100_000)
        self.assertAlmostEqual(by_year[2028], 100_000 * (1.03 ** 2), places=2)
        self.assertEqual(max(by_year), 2044)

    def test_closed_income_does_not_project_past_to(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 70_000,
                "from": "2010-01-01", "to": "2028-01-01",
            }],
            salary_growth=0.02,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        years = [e.year for e in entries]
        self.assertEqual(max(years), 2027)  # to year exclusive → 2027

    def test_fallback_grows_last_history_when_no_active_income(self):
        history = [{"year": y, "employment_income": 50_000} for y in range(1995, 2016)]
        entries = build_earnings_for_estimate(
            earnings_history=history,
            incomes=[],  # retired from employment
            salary_growth=0.02,
            as_of_year=2026,
            birth_year=1965,
            end_age=65,
        )
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2015], 50_000)
        # Grown from 2015 forward through 2029 (1965+65-1).
        self.assertEqual(max(by_year), 2029)
        self.assertAlmostEqual(by_year[2017], 50_000 * (1.02 ** 2), places=2)


class EstimatorBackwardPad(unittest.TestCase):
    """Issue #390: short careers pad BEFORE first year, not after last."""

    def test_no_zero_pad_after_last_year(self):
        # 30 years ending 2020 — old behavior padded 2021-2029 with zeros.
        entries = [
            EarningsEntry(year=y, employment_income=100_000)
            for y in range(1991, 2021)
        ]
        est = compute_benefit_estimate(entries, start_age=65)
        self.assertEqual(est.contributory_period_years, 40)
        # With backward pad, average should be higher than forward-zero-pad
        # because the 10 missing years sit before 1991 (still zeros, but the
        # last data year is retained as the span end). Spot-check non-zero.
        self.assertGreater(est.age_65_monthly, 0)

    def test_extended_series_beats_truncated_zero_pad(self):
        history = [
            EarningsEntry(year=y, employment_income=100_000)
            for y in range(2000, 2021)
        ]
        truncated = compute_benefit_estimate(history, start_age=65)
        extended = build_earnings_for_estimate(
            earnings_history=[
                {"year": e.year, "employment_income": e.employment_income}
                for e in history
            ],
            incomes=[{
                "kind": "employment", "amount": 100_000,
                "from": "2000-01-01", "to": None,
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        full = compute_benefit_estimate(extended, start_age=65)
        self.assertGreater(full.age_65_monthly, truncated.age_65_monthly)


class AlwaysOnFromIncomes(unittest.TestCase):
    """No Statement + employment incomes → non-zero cpp_monthly_estimated."""

    def test_incomes_alone_yield_estimate(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1.pop("earnings_history", None)
        self.assertTrue(any(i["kind"] == "employment" for i in p1["incomes"]))
        primary, _ = _primary(doc)
        self.assertGreater(primary["cpp_monthly_estimated"], 0)
        self.assertEqual(primary["cpp_benefit_source"], "estimated_from_incomes")
        self.assertEqual(primary.get("cpp_start_age"), 65)

    def test_salary_growth_changes_estimate(self):
        """Growth only moves the estimate when earnings sit below YMPE
        (above-YMPE careers are already ratio-capped)."""
        def _estimate(growth: float) -> float:
            doc = _two_generation_subset(_load_example())
            p1 = next(p for p in doc["people"] if p["id"] == "p1")
            _strip_statement(p1)
            p1.pop("earnings_history", None)
            # Below YMPE so future growth raises the average ratio.
            for inc in p1["incomes"]:
                if inc["kind"] == "employment":
                    inc["amount"] = 40_000
            doc["assumptions"]["salary_growth"] = growth
            primary, _ = _primary(doc)
            return primary["cpp_monthly_estimated"]

        low = _estimate(0.0)
        high = _estimate(0.05)
        self.assertGreater(high, low)

    def test_benefit_flows_at_claim_age(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1.pop("earnings_history", None)
        primary, _ = _primary(doc)
        # p1 born 1980; claim 65 => 2045.
        before = member_retirement_income(
            primary, 2043, oas_annual_max=8500, oas_clawback_threshold=90000)
        at_claim = member_retirement_income(
            primary, 2045, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertEqual(before.cpp, 0.0)
        self.assertGreater(at_claim.cpp, 0.0)



class BuildEarningsEdgeCases(unittest.TestCase):
    def test_skips_history_rows_missing_year_or_income(self):
        entries = build_earnings_for_estimate(
            earnings_history=[
                {"year": None, "employment_income": 50_000},
                {"year": 2010, "employment_income": None},
                {"year": 2011, "employment_income": 50_000},
            ],
            incomes=[],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1965,
            end_age=65,
        )
        years = [e.year for e in entries]
        self.assertIn(2011, years)
        self.assertNotIn(2010, years)

    def test_to_on_year_end_still_active_mid_year(self):
        """to=YYYY-12-31 remains active for an as_of earlier that year.

        Issue #415: the ACTIVE half of this assertion is unchanged -- a `to` of
        Dec 31 does not close the income before the as_of, so 2026 is still
        earned. The AMOUNT is now day-counted, because `amount` is an annual
        RATE (schema/defs/people.json) and `to` is exclusive: this window
        covers 2026-01-01 through 2026-12-30, which is 364 of 365 days. The
        assertion used to pin a whole annual rate onto a year the window only
        covers to within a day -- the ~2x overstatement #415 was filed for.
        """
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 70_000,
                "from": "2020-01-01", "to": "2026-12-31",
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
            as_of_date="2026-07-12",
        )
        by_year = {e.year: e.employment_income for e in entries}
        self.assertAlmostEqual(by_year[2026], 70_000 * 364 / 365)
        # Closed at year-end 2026 → no projection into 2027+.
        self.assertEqual(max(by_year), 2026)

    def test_overlay_includes_year_overlapping_half_open_to(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 50_000,
                "from": "2020-01-01", "to": "2026-01-01",
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
            as_of_date="2026-07-12",
        )
        by_year = {e.year: e.employment_income for e in entries}
        # [2020-01-01, 2026-01-01) overlaps 2025; inactive at as_of 2026-07-12
        # so 2026+ come from last-known fallback growth, not active income.
        self.assertEqual(by_year[2025], 50_000)
        self.assertEqual(max(by_year), 2044)

    def test_skips_income_missing_amount(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment",
                "from": "2015-01-01", "to": None,
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        self.assertEqual(entries, [])

    def test_skips_non_numeric_from(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 60_000,
                "from": "June 2026", "to": None,
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        self.assertEqual(entries, [])

    def test_skips_income_without_from(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 60_000,
                "from": None, "to": None,
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        self.assertEqual(entries, [])

    def test_skips_future_and_already_ended_incomes(self):
        entries = build_earnings_for_estimate(
            incomes=[
                {"kind": "employment", "amount": 40_000,
                 "from": "2030-01-01", "to": None},
                {"kind": "employment", "amount": 30_000,
                 "from": "2010-01-01", "to": "2020-01-01"},
                {"kind": "employment", "amount": 55_000,
                 "from": "2021-01-01", "to": None},
            ],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2026], 55_000)
        # Ended income still overlays its [from, to) years.
        self.assertEqual(by_year[2015], 30_000)
        # Future-start income is not active and does not add on top of 55k.
        self.assertEqual(by_year[2030], 55_000)

    def test_history_wins_over_projected_year(self):
        entries = build_earnings_for_estimate(
            earnings_history=[
                {"year": 2028, "employment_income": 12_000},
                {"year": 2020, "employment_income": 50_000},
            ],
            incomes=[{
                "kind": "employment", "amount": 50_000,
                "from": "2020-01-01", "to": None,
            }],
            salary_growth=0.10,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2028], 12_000)  # explicit history, not grown


class StatementStillWins(unittest.TestCase):
    def test_entitlements_ignore_incomes_estimate(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("earnings_history", None)
        p1["entitlements"] = {
            "cpp": {"estimated_monthly_at_65": 1111,
                    "as_of": "2026-01-01", "claim_age": 65},
        }
        primary, _ = _primary(doc)
        self.assertEqual(primary["cpp_monthly_estimated"], 1111)
        self.assertEqual(primary["cpp_benefit_source"], "statement")


class QuebecPlanSelection(unittest.TestCase):
    """QC residency uses QPP max-benefit table; YMPE stays CPP-centric."""

    def test_qpp_plan_changes_max_benefit_path(self):
        entries = [
            EarningsEntry(year=y, employment_income=80_000)
            for y in range(2000, 2045)
        ]
        cpp = compute_benefit_estimate(entries, start_age=65, plan="cpp")
        qpp = compute_benefit_estimate(entries, start_age=65, plan="qpp")
        # 2026 CPP max benefit (18092) != QPP (17334) — estimates diverge.
        self.assertNotEqual(cpp.age_65_monthly, qpp.age_65_monthly)

    def test_contract_path_selects_qpp_for_quebec_residency(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1.pop("earnings_history", None)
        self.assertEqual(p1["residency"]["province"], "quebec")
        primary_qc, _ = _primary(doc)

        p1["residency"] = {"province": "ontario", "since": "1980-03-14"}
        primary_on, _ = _primary(doc)
        # Same earnings path; different max-benefit table => different estimate.
        self.assertNotEqual(
            primary_qc["cpp_monthly_estimated"],
            primary_on["cpp_monthly_estimated"],
        )
        self.assertEqual(primary_qc["cpp_benefit_source"], "estimated_from_incomes")


class ConcurrentPensionableIncomes(unittest.TestCase):
    """A household can EARN two kinds of pensionable income at once.

    The overlay used to keep the FIRST income it saw for a year and discard the
    rest (``setdefault``), while the projection path summed -- so one household
    got two different answers inside a single series, and the discarded income
    vanished with no trace. That is the AGENTS.md trap ("returning the first
    match ... silently drops the rest") applied to a government entitlement:
    employment + self-employment understated the CPP estimate.
    """

    @staticmethod
    def _two_incomes():
        return [
            {"id": "emp", "kind": "employment", "amount": 35_000,
             "from": "2018-01-01", "to": None},
            {"id": "self", "kind": "self_employment", "amount": 25_000,
             "from": "2018-01-01", "to": None},
        ]

    def test_two_incomes_in_the_same_year_are_summed_not_dropped(self):
        entries = build_earnings_for_estimate(
            incomes=self._two_incomes(), salary_growth=0.0,
            as_of_year=2026, birth_year=1980, end_age=65)
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2018], 60_000)
        # ... and the as_of year, which the projection path fills, is summed too.
        self.assertEqual(by_year[2026], 60_000)

    def test_the_estimate_rises_when_the_second_income_is_counted(self):
        """Relational, not a snapshot: the summed series cannot price the same
        as the dropped one."""
        both = build_earnings_for_estimate(
            incomes=self._two_incomes(), salary_growth=0.0,
            as_of_year=2026, birth_year=1980, end_age=65)
        one = build_earnings_for_estimate(
            incomes=self._two_incomes()[:1], salary_growth=0.0,
            as_of_year=2026, birth_year=1980, end_age=65)
        est_both = compute_benefit_estimate(both, start_age=65)
        est_one = compute_benefit_estimate(one, start_age=65)
        self.assertGreater(est_both.age_65_monthly, est_one.age_65_monthly)

    def test_declared_history_still_wins_over_an_income_derived_year(self):
        """History precedence is unchanged: a declared figure is never inflated
        by an income landing on the same year."""
        entries = build_earnings_for_estimate(
            earnings_history=[{"year": y, "employment_income": 100_000}
                              for y in range(2018, 2021)],
            incomes=self._two_incomes()[:1], salary_growth=0.0,
            as_of_year=2026, birth_year=1980, end_age=65)
        by_year = {e.year: e.employment_income for e in entries}
        self.assertEqual(by_year[2019], 100_000)


class BuildEarningsGuards(unittest.TestCase):
    def test_skips_non_digit_to_in_overlay(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 60_000,
                "from": "2015-01-01", "to": "not-a-date",
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
        )
        self.assertEqual(entries, [])

    def test_skips_active_income_with_non_digit_to(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 60_000,
                "from": "2015-01-01", "to": "June 2030",
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
            as_of_date="2026-07-12",
        )
        self.assertEqual(entries, [])


class ProjectedEarningsKey(unittest.TestCase):
    def test_incomes_only_uses_cpp_estimated_earnings_key(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1.pop("earnings_history", None)
        primary, _ = _primary(doc)
        self.assertNotIn("earnings_history", primary)
        self.assertIn("cpp_estimated_earnings", primary)
        self.assertGreater(len(primary["cpp_estimated_earnings"]), 0)

    def test_declared_history_not_replaced_by_projection(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        hist = [{"year": 1990 + i, "employment_income": 100_000} for i in range(35)]
        p1["earnings_history"] = hist
        primary, _ = _primary(doc)
        self.assertEqual(len(primary["earnings_history"]), 35)
        self.assertNotIn("cpp_estimated_earnings", primary)


class ClosedIncomeJan1(unittest.TestCase):
    def test_to_on_jan_1_last_project_year_is_prior(self):
        entries = build_earnings_for_estimate(
            incomes=[{
                "kind": "employment", "amount": 70_000,
                "from": "2020-01-01", "to": "2028-01-01",
            }],
            salary_growth=0.0,
            as_of_year=2026,
            birth_year=1980,
            end_age=65,
            as_of_date="2026-07-12",
        )
        years = [e.year for e in entries]
        self.assertEqual(max(years), 2027)  # [..., 2028-01-01) → through 2027


class MissingResidencyRefuses(unittest.TestCase):
    def test_estimate_without_residency_raises(self):
        from contract_people import _map_member
        from contract_errors import ContractAdaptationError
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("benefits", None)
        p1.pop("earnings_history", None)
        p1["residency"] = None
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map_member(doc, "p1", "primary", {})
        self.assertIn("residency.province", str(ctx.exception))


class QppMaxBenefitFallback(unittest.TestCase):
    def test_qpp_year_before_table_falls_through_to_the_cpp_maximum(self):
        from countries.canada.cpp_data import max_benefit_65_for_year
        # Exact table year.
        self.assertEqual(max_benefit_65_for_year(2026, plan="qpp"), 17334)
        # Before QPP table → fall through to CPP historical (not 2023 QPP).
        before = max_benefit_65_for_year(2020, plan="qpp")
        cpp_2020ish = max_benefit_65_for_year(2020, plan="cpp")
        self.assertEqual(before, cpp_2020ish)
        # Future year after table → latest QPP row.
        self.assertEqual(max_benefit_65_for_year(2035, plan="qpp"), 17334)


class HistoryPlusGrowth(unittest.TestCase):
    def test_history_source_provenance(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1["earnings_history"] = [
            {"year": 1990 + i, "employment_income": 100_000} for i in range(35)
        ]
        primary, _ = _primary(doc)
        self.assertGreater(primary["cpp_monthly_estimated"], 0)
        self.assertEqual(
            primary["cpp_benefit_source"], "estimated_from_earnings_history",
        )
        # Declared history length preserved (extension is ephemeral).
        self.assertEqual(len(primary["earnings_history"]), 35)


if __name__ == "__main__":
    unittest.main()
