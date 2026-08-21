"""Epic #795 bite 3 — the ITA s.146(1) earned-income classification moves out
of the generic fold (`simulation.py`) into the Canada jurisdiction module.

Bite 3's verdict: the dated income-segment / income-shock *evaluation*
(`_income_components_for_year`, `_income_shock_active_for_year`) is generic
income arithmetic — it derives a year's income from stored `[from, to)` windows
by day count, exactly the way `age_in(year)` is derived from a birth date
(DP#1). It stays in the fold as an intentionally-core primitive.

The ONE program-shaped fragment entangled in that generic code is the earned-
income classification: ITA s.146(1) decides which income `kind` accrues RRSP
room. That is Canadian tax law and belongs in `countries.canada` (DP#10 — one
module per program; DP#25 — the generic engine module must not encode a
jurisdiction's tax law). This test pins that the classification now lives in
`countries.canada.earned_income` and that `simulation.py` no longer defines it
(migrated, not duplicated — DP#9).
"""

import inspect
import unittest

from countries.canada.earned_income import (
    is_earned_income,
    EARNED_INCOME_KINDS,
    NON_EARNED_INCOME_KINDS,
)


class TestEarnedIncomeClassification(unittest.TestCase):
    def test_employment_and_self_employment_are_earned(self):
        # ITA s.146(1): employment + net self-employment accrue RRSP room.
        self.assertTrue(is_earned_income("employment"))
        self.assertTrue(is_earned_income("self_employment"))

    def test_ei_investment_rental_other_are_not_earned(self):
        for kind in ("ei", "investment", "rental", "other"):
            self.assertFalse(is_earned_income(kind), kind)

    def test_unclassified_kind_raises_not_silently_false(self):
        # DP#32: an unclassified kind must raise, never default to unearned
        # (silently understating RRSP room is the #674 defect, mirrored).
        with self.assertRaises(ValueError):
            is_earned_income("salary")  # plausible typo for "employment"

    def test_sets_partition_the_classification(self):
        # No kind is both earned and not; the union is what makes it total.
        self.assertEqual(
            EARNED_INCOME_KINDS & NON_EARNED_INCOME_KINDS, frozenset()
        )


class TestMigratedNotDuplicated(unittest.TestCase):
    """DP#9 / DP#25: the classification left the generic fold module — it is
    not defined there, and not imported at module scope either."""

    def test_simulation_module_does_not_redefine_the_classification(self):
        import simulation

        src = inspect.getsource(simulation)
        self.assertNotIn(
            "EARNED_INCOME_KINDS = frozenset", src,
            "simulation.py must not re-declare the ITA s.146(1) earned-income "
            "classification — it belongs in countries.canada.earned_income "
            "(DP#10/DP#25). Import it, don't redefine it.",
        )

    def test_simulation_does_not_expose_classifier_at_module_scope(self):
        # DP#25: the generic fold must not carry a jurisdiction symbol at module
        # scope; the classifier is lazy-imported inside the year step. (The
        # test_jurisdiction_agnostic guard forbids the module-level import.)
        import simulation

        self.assertFalse(hasattr(simulation, "EARNED_INCOME_KINDS"))

    def test_fold_routes_segment_kind_through_migrated_classifier(self):
        # The fold's income-segment blending still classifies a segment's kind:
        # an unclassified kind must raise via the migrated ITA s.146(1) rule.
        from simulation import _income_components_for_year

        with self.assertRaises(ValueError):
            _income_components_for_year(
                base_amount=0.0,
                segments=[{"kind": "salary", "amount": 50_000,
                           "from": "2030-01-01", "to": None}],
                calendar_year=2030, salary_growth=0.0, year_index=0)


if __name__ == "__main__":
    unittest.main()
