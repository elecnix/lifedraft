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
        # rate 1.0: after-tax income is saved, so it lands in
        # total_assets -- the one tax-sensitive figure YearResult exposes.
        "savings": {"rate": 1.0},
        "tax": {"province": province},
    }


def _first_year_tax(employment_income, province="ontario", year=2026):
    """Tax the FOLD actually charged, observed end to end.

    ``YearResult`` exposes no income-tax or after-tax field -- both live
    inside the fold. Two earlier versions of this helper therefore read
    non-existent attributes behind ``getattr(..., 0.0)`` and reported **zero
    tax at every income**, which made every assertion built on it vacuous.

    The observable is ``total_assets`` with a savings rate of 1.0: after-tax
    income is then saved whole, so ``assets = income - tax`` and the tax is
    recoverable from a field that genuinely exists.
    """
    cfg = SimulationConfig.from_dict(
        _household(employment_income, province, year))
    results = FamilySimulation(cfg).run()
    assert results, "the fold produced no years"
    yr = results[0]
    assets = float(getattr(yr, "total_assets", 0.0) or 0.0)
    return employment_income - assets, yr


class TestFoldAppliesNonRefundableCredits(unittest.TestCase):
    """The credits must be visible in the YEAR RESULT, not just in a helper."""

    def test_a_single_employee_pays_less_than_the_bracket_tax(self):
        from tax_data import default_tax_provider
        from tax_calculator import tax_on_income

        income = 60_000.0
        paid, _ = _first_year_tax(income)

        brackets = default_tax_provider().get_combined_brackets(
            year=2026, province="ontario")
        bracket_only = tax_on_income(income, brackets)

        self.assertGreater(bracket_only, paid, (
            "the fold charged bracket tax with no credits applied; the "
            "non-refundable credits must reduce it"
        ))
        # The relief is material, not a rounding artefact.
        self.assertGreater(bracket_only - paid, 1_000.0)

    def test_every_income_level_gets_relief(self):
        """The BPA is worth most as a SHARE of tax at low incomes.

        Stated as a share rather than a dollar amount, because the credit is
        a fixed dollar amount multiplied by a rate -- so in absolute terms it
        is roughly flat while tax grows, and an earlier version of this test
        asserted a decreasing absolute amount and failed. The share is what
        actually falls.
        """
        from tax_data import default_tax_provider
        from tax_calculator import tax_on_income

        brackets = default_tax_provider().get_combined_brackets(
            year=2026, province="ontario")
        shares = []
        for income in (20_000.0, 60_000.0, 100_000.0):
            paid, _ = _first_year_tax(income)
            bracket_only = tax_on_income(income, brackets)
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
        """Abated is not zeroed: the credit still lands."""
        paid, _ = _first_year_tax(60_000.0, province="quebec")
        self.assertGreater(paid, 0.0)
        self.assertLess(paid, 15_777.0)


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
