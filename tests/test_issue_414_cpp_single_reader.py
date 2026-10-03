"""Issue #414: one reader of ``cpp_monthly_estimated``, one age factor.

``cpp_monthly_estimated`` is the statement-**at-65** monthly estimate. The fold
prices it through ``retirement_transition.cpp_from_estimate`` -- x12, the
claim-age factor, a ``[60, 70]`` clamp, and a gate that pays $0 until the member
reaches ``cpp_start_age``. ``objective.compute_net_benefit`` used to build the
terminal capital-gain leg's marginal-rate base from the SAME key by a second
derivation (``primary['cpp_monthly_estimated'] * 12``) that applied none of the
three. The optimizer's default objective therefore ranked strategies against a
retirement-income base up to 42% away from the one the projection actually pays,
in a direction that flips with the member's declared claim age.

These tests are end-to-end on purpose (DP#11): they drive
``FamilySimulation.run()`` and assert on ``YearResult`` and on the objective's
own ``marginal_rate`` call. Nothing here builds engine state by hand, and nothing
re-implements the engine to check it against itself -- a test that does that
passes while the engine is broken, which is the trap AGENTS.md names.

A member claiming at 70 is the least-proven configuration in the whole CPP path
(before this file no test anywhere exercised ``cpp_start_age != 65`` through the
fold), so the first class drives exactly that end to end.

The fixture is the shipped example document, loaded the way the CLI loads it --
``input_contract.to_internal_config`` -> ``SimulationConfig.from_dict`` ->
``FamilySimulation.run()``. The spouse's pensionable income and earnings history
are removed so the household's ``cpp_income`` is the primary's alone:
``cpp_income`` is the family's combined CPP/QPP, and a second contributor makes
the primary's claim-age behaviour unobservable in the row. Every figure is a
fabricated round number and every name is role-based (DP#4/DP#15).
"""
from __future__ import annotations

import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract
import objective as objective_module
from countries.canada.adapter import CanadaAdapter
from objective import compute_net_benefit, objective_cfg
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_input_contract import _load_example, _two_generation_subset

# Round, fabricated statement figures (DP#15).
_MONTHLY_AT_65 = 1000.0
# The statutory per-month factors, restated here to state what the law SAYS the
# engine must produce. This is the assertion's own arithmetic, not a second
# implementation of the engine: the test states the law, then asks the engine
# whether it agrees.
_EARLY_PENALTY_PER_MONTH = 0.006
_LATE_BONUS_PER_MONTH = 0.007
_MAX_MONTHS = 12


def _doc_with_claim(claim_age: int, until_age: int = 95,
                    monthly_at_65: float = _MONTHLY_AT_65) -> dict:
    """The example document, with the primary declaring a CPP entitlement that
    has NOT started yet (``entitlements.cpp``), claiming at ``claim_age``."""
    doc = _two_generation_subset(_load_example())

    primary = next(p for p in doc["people"] if p["id"] == "p1")
    primary.pop("benefits", None)
    primary.pop("entitlements", None)
    primary["entitlements"] = {
        "cpp": {
            "estimated_monthly_at_65": monthly_at_65,
            "as_of": "2026-01-01",
            "claim_age": claim_age,
        },
    }

    # The spouse keeps no pensionable income and declares no earnings history,
    # so the always-on CPP estimate (#390) has nothing to estimate FROM and the
    # household's cpp_income is the primary's alone.
    spouse = next(p for p in doc["people"] if p["id"] == "p2")
    spouse.pop("benefits", None)
    spouse.pop("entitlements", None)
    spouse.pop("earnings_history", None)
    spouse["incomes"] = [
        i for i in spouse.get("incomes", [])
        if i.get("kind") not in ("employment", "self_employment")
    ]

    doc["decisions"]["horizon"] = {"person": "p1", "until_age": until_age}
    return doc


def _config(doc: dict) -> SimulationConfig:
    return SimulationConfig.from_dict(input_contract.to_internal_config(doc))


def _run(doc: dict):
    """One real fold. The adapter logs load-time notices rather than warning,
    so the filter only satisfies pytest's ``warnings`` -> error config."""
    config = _config(doc)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = FamilySimulation(config, adapter=CanadaAdapter(config)).run()
    return config, results


def _primary_member(config: SimulationConfig) -> dict:
    return next(m for m in config.family_members if m["role"] == "primary")


def _calendar_year(config: SimulationConfig, row) -> int:
    """The calendar year a ``YearResult`` row describes.

    ``YearResult.year`` is a 1-indexed RELATIVE offset (``objective_cfg``'s own
    docstring says so); the absolute year is the start year plus that offset
    minus one (DP#1: store dates, not derived offsets).
    """
    return config.start_year + row.year - 1


class ClaimAgeReachesTheFold(unittest.TestCase):
    """``entitlements.cpp.claim_age != 65``, end to end.

    Before this file nothing exercised a non-65 claim age through the fold: the
    contract-side tests stopped at the mapped member dict, which is exactly the
    "parsed, mapped, then never passed" shape -- the adapter reading a leaf
    proves nothing about the engine honouring it.
    """

    def _only_the_primary_has_cpp(self, doc: dict) -> None:
        cfg = input_contract.to_internal_config(doc)
        contributors = [m for m in cfg["family"]["members"]
                        if m.get("cpp_monthly_estimated")]
        self.assertEqual(
            [m["role"] for m in contributors], ["primary"],
            "fixture must leave the primary as the only CPP contributor, or "
            "cpp_income (the family's combined figure) cannot show the "
            "primary's claim-age behaviour",
        )

    def test_a_claim_at_70_pays_the_late_start_bonus(self):
        doc = _doc_with_claim(70)
        self._only_the_primary_has_cpp(doc)
        config, results = _run(doc)

        member = _primary_member(config)
        self.assertEqual(member["cpp_start_age"], 70)
        self.assertEqual(member["cpp_monthly_estimated"], _MONTHLY_AT_65)

        paid = [r for r in results if r.cpp_income > 0]
        self.assertTrue(paid, "no year of the fold paid any CPP")
        expected = _MONTHLY_AT_65 * _MAX_MONTHS * (
            1 + 5 * _MAX_MONTHS * _LATE_BONUS_PER_MONTH)

        for row in paid:
            self.assertAlmostEqual(
                row.cpp_income, expected, delta=1.0,
                msg="the claim-at-70 amount must be the age-adjusted one",
            )
        self.assertNotAlmostEqual(
            paid[-1].cpp_income, _MONTHLY_AT_65 * _MAX_MONTHS, delta=1.0,
            msg="the raw statement-at-65 figure x12 is not the claim-at-70 "
                "benefit: the engine applied no age factor",
        )

    def test_a_claim_at_60_pays_the_early_start_penalty(self):
        """The other side of the same factor -- the two must not be one-sided."""
        doc = _doc_with_claim(60)
        self._only_the_primary_has_cpp(doc)
        config, results = _run(doc)

        member = _primary_member(config)
        self.assertEqual(member["cpp_start_age"], 60)
        paid = [r for r in results if r.cpp_income > 0]
        self.assertTrue(paid)
        expected = _MONTHLY_AT_65 * _MAX_MONTHS * (
            1 - 5 * _MAX_MONTHS * _EARLY_PENALTY_PER_MONTH)
        self.assertAlmostEqual(paid[-1].cpp_income, expected, delta=1.0)

    def test_nothing_is_paid_before_the_claim_age(self):
        """The gate. A claim age is a DATE, not a hint (DP#1/#28): the member
        who has declared a $1,000/month statement claiming at 70 is paid
        NOTHING in any year before they turn 70."""
        for claim_age in (60, 70):
            with self.subTest(claim_age=claim_age):
                doc = _doc_with_claim(claim_age)
                config, results = _run(doc)
                member = _primary_member(config)
                birth_year = member["birth_year"]
                for row in results:
                    age = _calendar_year(config, row) - birth_year
                    if age < claim_age:
                        self.assertEqual(
                            row.cpp_income, 0.0,
                            f"CPP paid in year {row.year} at age {age}, "
                            f"before the declared claim age {claim_age}",
                        )
                    else:
                        self.assertGreater(row.cpp_income, 0.0)

    def test_the_fold_prices_a_claim_never_reached_as_zero(self):
        """A horizon that ends before the claim age pays nothing at all --
        which is exactly what the optimizer used to contradict."""
        doc = _doc_with_claim(70, until_age=68)
        config, results = _run(doc)
        member = _primary_member(config)
        self.assertEqual(member["cpp_start_age"], 70)
        self.assertEqual(
            [r.cpp_income for r in results], [0.0] * len(results),
            "a claim at 70 inside a horizon to 68 must pay nothing",
        )


class TheOptimizerReadsTheFold(unittest.TestCase):
    """``compute_net_benefit`` must price the CG leg off the fold's own number.

    The CG leg's ``marginal_rate`` is looked up on a retirement-income base, so
    a base built from the wrong CPP moves ``net_benefit`` -- which is the
    optimizer's default ranking score (``voi.DEFAULT_OBJECTIVE_NAME``). These
    tests spy on the ONE ``marginal_rate`` call ``compute_net_benefit`` makes and
    assert what base it was handed, so nothing about the engine is re-implemented
    in order to check the engine.
    """

    def _recorded_cg_base(self, doc: dict):
        """Run the fold, then ``compute_net_benefit`` with ``marginal_rate``
        spied on. Returns ``(config, cfg, final_row, recorded_base)``."""
        config, results = _run(doc)
        cfg = objective_cfg(config)

        recorded = []
        real = objective_module.marginal_rate

        def spy(income, brackets, *args, **kwargs):
            recorded.append(income)
            return real(income, brackets, *args, **kwargs)

        objective_module.marginal_rate = spy
        try:
            compute_net_benefit(results, cfg)
        finally:
            objective_module.marginal_rate = real

        self.assertEqual(
            len(recorded), 1,
            "expected exactly one marginal_rate call from the CG leg; the "
            "spy's subject moved and this test would prove nothing",
        )
        return config, cfg, results[-1], recorded[0]

    def _cpp_term_in_base(self, cfg, final_row, base) -> float:
        """The recorded base less the three legs that are NOT the CPP term, so
        what remains is provably the CPP contribution. Each subtraction reads a
        value the module itself reads -- none is re-derived."""
        assumptions = cfg.get("assumptions", {})
        oas = (assumptions["oas_annual"] if "oas_annual" in assumptions
               else objective_module._default_oas_annual(cfg))
        primary = objective_module.find_member_by_role(
            cfg["family"]["members"], "primary", {})
        pension = primary.get("pension_income_annual", 0)
        lif = getattr(final_row, "lif_withdrawal", 0)
        return base - oas - pension - lif

    def test_the_cg_base_carries_the_folds_cpp_not_a_re_derivation(self):
        doc = _doc_with_claim(70)
        config, cfg, final_row, base = self._recorded_cg_base(doc)
        cpp_in_base = self._cpp_term_in_base(cfg, final_row, base)

        self.assertGreater(final_row.cpp_income, 0.0, "fixture must pay CPP")
        self.assertAlmostEqual(cpp_in_base, final_row.cpp_income, delta=0.01)

        # The pre-#414 read, spelled out, and how far it was off.
        primary = _primary_member(config)
        old_base = primary["cpp_monthly_estimated"] * _MAX_MONTHS
        self.assertNotAlmostEqual(
            cpp_in_base, old_base, delta=1.0,
            msg="the CG base is still the raw statement-at-65 x12: no age "
                "factor, no claim-age gate",
        )
        # A claim at 70 is worth 1.42x the at-65 figure, so the old read was
        # short by nearly a third of the primary's real CPP.
        self.assertGreater(cpp_in_base, old_base)

    def test_the_cg_base_is_zero_when_the_fold_pays_no_cpp(self):
        """An unclaimed member's statement is not income. The old read charged
        it anyway, inflating the CG base in every year before the claim age."""
        doc = _doc_with_claim(70, until_age=68)
        config, cfg, final_row, base = self._recorded_cg_base(doc)
        self.assertEqual(final_row.cpp_income, 0.0, "fixture pays no CPP")
        self.assertAlmostEqual(
            self._cpp_term_in_base(cfg, final_row, base), 0.0, delta=0.01,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()