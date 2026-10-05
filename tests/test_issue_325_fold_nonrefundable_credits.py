"""#325: the fold must apply each adult's non-refundable tax credits.

The fold taxed BRACKETS ONLY. `simulation._income_tax_by_adult` computed
``tax_before = tax_on_income(taxable, brackets)`` -- its own docstring calls
it the *pre-credit* tax -- and nothing ever subtracted the basic personal
amount, the Canada employment amount, or the other non-refundable credits.
Disposable income was therefore ``income - tax_before``, overstating tax and
understating savings capacity, runway and solvency in **every year with
income**.

Measured on a fabricated Quebec employee, 2026, before the fix:

| employment income | fold (brackets only) | with credits | overstatement |
|---|---|---|---|
| 30,000 | 7,707 | 4,295 | **+79%** |
| 60,000 | 15,777 | 13,185 | +2,592 |
| 100,000 | 30,225 | 28,140 | +2,084 |

These tests drive ``FamilySimulation.run()`` end to end rather than calling
the tax helper directly. That is the point of the issue: a unit test on a
pricing function would pass while the FOLD kept omitting the credit -- which
is exactly the shortcut AGENTS.md warns about, and exactly what happened for
years here.
"""
import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig


def _household(employment_income, province="ontario", year=2026):
    """One adult, no spouse, no children, no living costs."""
    return {
        "family": {
            "members": [{
                "role": "primary", "birth_year": year - 30,
                "gross_income": employment_income,
            }],
        },
        "assumptions": {
            "start_year": year, "horizon_age": 1,
            "investment_return": 0.0, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True,
        },
        "savings": {"rate": 1.0},
        "tax": {"province": province},
        # Issue #679: the solvency identity -- and with it
        # ``YearResult.after_tax_income`` -- only runs when a real obligation
        # is declared (``ctx.living_costs > 0``, or a segment/purchase/carrying
        # cost). Without this the observable below stays 0.0 and every
        # assertion in this file would compare against income itself.
        "household_budget": {"living_costs": 30_000.0},
    }


def _fold(employment_income, province="ontario", year=2026):
    """Run the fold for one adult; return ``(sim, first_year_result)``."""
    sim = FamilySimulation(SimulationConfig.from_dict(
        _household(employment_income, province, year)))
    results = sim.run()
    assert results, "the fold produced no years"
    return sim, results[0]


def _first_year_tax(employment_income, province="ontario", year=2026):
    """Tax the FOLD actually charged, observed end to end.

    ``YearResult`` exposes no income-tax field, so this reads the one that
    genuinely responds to a credit: ``after_tax_income``, which
    ``apply_solvency`` fills from the prologue's ``_after_tax_by_role`` =
    income - tax_before (simulation.py:1445). Read directly, with no
    ``getattr`` default: an absent field must raise rather than become a
    plausible number (DP#32).

    Two earlier versions of this helper read non-existent attributes and so
    reported **zero tax at every income**. A third read ``total_assets``, which
    is not tax on this path at all: the primary-couple fold computes
    ``annual_savings = total_income * cfg.savings_rate`` (simulation.py:1315),
    so the figure is tax-BLIND. Measured: 433.44 for a $60,000 employee in BOTH
    Ontario and Quebec, unchanged when ``fold_non_refundable_credit`` is
    patched to 0.0 or to 50,000.0. Hence ``after_tax_income``.

    ``_fold`` returning the simulation as well matters for the bracket
    comparisons below: ``sim.brackets`` is the frozen list the fold actually
    taxed from, and it is NOT the same list a fresh
    ``get_combined_brackets(province=...)`` call returns for a hand-built
    internal config. Comparing against the latter measured a different bracket
    list than the one under test.
    """
    _, yr = _fold(employment_income, province, year)
    return employment_income - float(yr.after_tax_income), yr


class TestFoldAppliesNonRefundableCredits(unittest.TestCase):
    """The credits must be visible in the YEAR RESULT, not just in a helper."""

    def test_a_single_employee_pays_less_than_the_bracket_tax(self):
        from tax_calculator import tax_on_income

        income = 60_000.0
        sim, yr = _fold(income)
        paid = income - float(yr.after_tax_income)

        # The fold's OWN frozen bracket list, not a fresh provider lookup: a
        # hand-built internal config never threads its declared province into
        # ``SimulationConfig.brackets``, so the two are different lists.
        bracket_only = tax_on_income(income, sim.brackets)

        self.assertGreater(bracket_only, paid, (
            "the fold charged bracket tax with no credits applied; the "
            "non-refundable credits must reduce it"
        ))
        # The relief is material, not a rounding artefact.
        self.assertGreater(bracket_only - paid, 1_000.0)
        # ... and it is EXACTLY the credit the fold prices, so a regression
        # that halves the credit fails here rather than merely shrinking.
        from countries.canada.tax_calc import fold_non_refundable_credit
        self.assertAlmostEqual(
            bracket_only - paid,
            fold_non_refundable_credit(_config("ontario"), income, income),
            places=6,
        )

    def test_every_income_level_gets_relief(self):
        """The BPA is worth most as a SHARE of tax at low incomes.

        Stated as a share rather than a dollar amount, because the credit is
        a fixed dollar amount multiplied by a rate -- so in absolute terms it
        is roughly flat while tax grows, and an earlier version of this test
        asserted a decreasing absolute amount and failed. The share is what
        actually falls.
        """
        from tax_calculator import tax_on_income

        shares = []
        for income in (20_000.0, 60_000.0, 100_000.0):
            sim, yr = _fold(income)
            paid = income - float(yr.after_tax_income)
            bracket_only = tax_on_income(income, sim.brackets)
            self.assertGreater(bracket_only, paid, income)
            shares.append((bracket_only - paid) / bracket_only)
        self.assertGreater(shares[0], shares[-1], (
            "relief as a share of tax must be largest at the lowest income"
        ))

    def test_a_zero_income_adult_owes_nothing(self):
        """Credits cannot make tax payable negative (DP#32)."""
        paid, _ = _first_year_tax(0.0)
        self.assertGreaterEqual(paid, 0.0)

    def test_the_credit_is_reported_alongside_the_pre_credit_tax(self):
        """The reduction is inspectable, not implicit.

        ``tax_before_credits`` and ``non_refundable_credits`` travel with the
        per-adult tax so a household (and a reviewer) can see WHICH credits
        moved the number.
        """
        from simulation import _income_tax_by_adult

        from simulation_config import SimulationConfig as SC

        cfg = SC.from_dict(_household(60_000.0))
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets(
            year=2026, province="ontario")
        per_adult = _income_tax_by_adult(
            cfg, {"primary": 60_000.0, "spouse": 0.0},
            {"primary": (0.0, 0.0), "spouse": (0.0, 0.0)}, brackets)
        slot = per_adult["primary"]
        self.assertGreater(slot["non_refundable_credits"], 0.0)
        self.assertAlmostEqual(
            slot["tax_before"],
            slot["tax_before_credits"] - slot["non_refundable_credits"],
            places=6,
        )


class TestQuebecCreditIsAbated(unittest.TestCase):
    """A Quebec resident's federal credit is worth rate x (1 - 0.165).

    The refundable Quebec abatement is computed on line 42900 -- BASIC
    federal tax, i.e. AFTER the federal credits (ITA s.120(4)) -- so each
    dollar of credit shrinks the abatement by 16.5% of itself. Subtracting
    the un-abatemented credit from an already-abatemented combined tax would
    overstate the relief by exactly that much (#348, #350).
    """

    def test_the_quebec_credit_is_exactly_the_ontario_credit_less_the_abatement(self):
        """QC credit = ON credit x (1 - 0.165), to the cent.

        Compared on the CREDIT, not on the relief. An earlier version
        compared relief across provinces and asserted it was smaller in
        Quebec -- but the two provinces' combined brackets differ, so that
        comparison measured the brackets, not the abatement, and failed.
        The credit itself is the quantity the statute scales.
        """
        from countries.canada.tax_calc import fold_non_refundable_credit as _nrc

        income, taxable = 60_000.0, 60_000.0
        on_credit = _nrc(
            _config("ontario"), income, taxable)
        qc_credit = _nrc(
            _config("quebec"), income, taxable)

        self.assertGreater(on_credit, 0.0)
        self.assertAlmostEqual(qc_credit, on_credit * (1 - 0.165), places=6)

    def test_the_quebec_household_still_pays_less_than_its_bracket_tax(self):
        """Abated is not zeroed: the credit still lands, and it is the exact
        credited figure -- not merely 'below some bracket number'.

        The earlier version of this test asserted ``paid < 15_777.0``, where
        15,777 was the PRE-credit tax: any credit at all, correct or wildly
        too large, satisfied it, so it could not fail for the reason it was
        named. It now measures the relief itself.
        """
        from tax_calculator import tax_on_income
        from countries.canada.tax_calc import fold_non_refundable_credit

        income = 60_000.0
        sim, yr = _fold(income, province="quebec")
        paid = income - float(yr.after_tax_income)
        relief = tax_on_income(income, sim.brackets) - paid

        self.assertGreater(paid, 0.0)
        # Material, not an epsilon: a zeroed credit leaves ~1.8e-12 of float
        # noise here, which ``> 0`` would happily accept.
        self.assertGreater(relief, 1_000.0)
        self.assertAlmostEqual(
            relief,
            fold_non_refundable_credit(_config("quebec"), income, income),
            places=6,
        )

    def test_the_abatement_is_the_ontario_relief_times_0_835(self):
        """The 16.5% abatement, seen end to end in the FOLD's own numbers.

        Both households are taxed from the same frozen bracket list, so the
        only difference between the two reliefs is the abatement the Canada
        package applies to the federal credit. The helper-level test above
        pins the same fact on ``fold_non_refundable_credit`` directly; this one
        pins that the fold actually USES it -- the wiring, not the arithmetic.
        """
        from tax_calculator import tax_on_income

        income = 60_000.0
        on_sim, on_yr = _fold(income, province="ontario")
        qc_sim, qc_yr = _fold(income, province="quebec")
        on_relief = tax_on_income(income, on_sim.brackets) - (
            income - float(on_yr.after_tax_income))
        qc_relief = tax_on_income(income, qc_sim.brackets) - (
            income - float(qc_yr.after_tax_income))

        self.assertGreater(on_relief, 1_000.0, (
            "a zeroed credit leaves float noise here, so this must be a "
            "material floor, not ``> 0``"
        ))
        self.assertAlmostEqual(qc_relief, on_relief * (1 - 0.165), places=6)


def _config(province):
    return SimulationConfig.from_dict(_household(60_000.0, province))


class TestTheFoldIsNotBackwards(unittest.TestCase):
    """A credit must never INCREASE tax -- the regression that matters."""

    def test_raising_income_never_lowers_the_credited_tax(self):
        previous = None
        for income in (0.0, 20_000.0, 40_000.0, 60_000.0, 100_000.0, 200_000.0):
            paid, _ = _first_year_tax(income)
            self.assertGreaterEqual(paid, 0.0, income)
            if previous is not None:
                self.assertGreaterEqual(paid, previous - 1e-6, (
                    f"tax fell when income rose to {income}: a credit is "
                    f"being applied outside its declared income envelope"
                ))
            previous = paid


if __name__ == "__main__":
    unittest.main()
