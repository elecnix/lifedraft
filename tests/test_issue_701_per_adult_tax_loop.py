"""Issue #701 (Step 5 of #643): tax each adult individually via a loop.

The two-role taxable-income/tax block in ``simulation.py`` now iterates
``config.adults()`` (``_income_tax_by_adult``) instead of a hardcoded
primary/spouse pair. This step is purely ISOMORPHIC for the two-adult golden
household -- each adult is still taxed in their OWN bracket (Canada has no joint
filing), the loop just makes that explicit and generalizes to N adults. These
lean unit tests pin the invariants the loop must hold; the golden-trajectory
invariant (``test_golden_trajectory_581``) guards the end-to-end isomorphism.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simulation import _income_tax_by_adult  # noqa: E402
from tax_calculator import marginal_rate, tax_on_income  # noqa: E402


# A tiny stand-in for the config seam the helper depends on -- it only calls
# ``config.adults()`` (issue #699), so a stub returning role dicts is enough to
# exercise the loop without building a whole SimulationConfig.
class _StubConfig:
    """Minimal stand-in for SimulationConfig.

    Issue #325: the fold's tax step now also reads ``start_year`` and
    ``province`` to price the adult's non-refundable credits, so the stub
    carries them. A stub that omits a field the production function reads is
    the stub being wrong, not the function being defensive.
    """

    def __init__(self, roles, start_year=2026, province="ontario"):
        self._roles = roles
        self.start_year = start_year
        self.province = province

    def adults(self):
        return [{'role': r} for r in self._roles]


def _expected_tax(cfg, income, taxable, brackets):
    """What the fold must charge this adult.

    Issue #325 changed the invariant, so the expectation changed with it:
    the per-adult tax is now the bracket tax MINUS that adult's non-refundable
    credits, not the bracket tax alone.

    The old assertion was not "wrong" in its own terms -- it pinned exactly
    what the fold did before #325. It is the SUBJECT that is preserved here
    (one adult, one private-loan adjustment, one bracket set, each taxed
    separately); only the credit term is new, and it is computed by the same
    production helper the fold calls, so this test cannot drift from it.
    """
    from countries.canada.tax_calc import fold_non_refundable_credit

    credit = fold_non_refundable_credit(cfg, income, taxable)
    return max(0.0, tax_on_income(taxable, brackets) - credit)


BRACKETS = [
    {'min': 0, 'max': 50_000, 'rate': 0.20},
    {'min': 50_000, 'max': 100_000, 'rate': 0.35},
    {'min': 100_000, 'max': None, 'rate': 0.50},
]


def test_each_adult_taxed_in_their_own_bracket():
    """No joint filing: each adult's rate/tax fall on their OWN income."""
    cfg = _StubConfig(['primary', 'spouse'])
    incomes = {'primary': 120_000, 'spouse': 40_000}
    loans = {'primary': (0.0, 0.0), 'spouse': (0.0, 0.0)}

    result = _income_tax_by_adult(cfg, incomes, loans, BRACKETS)

    for role, income in incomes.items():
        assert result[role]['rate'] == marginal_rate(income, BRACKETS)
        assert result[role]['tax_before'] == pytest.approx(
            _expected_tax(cfg, income, income, BRACKETS))
    # Individual, not joint: summed tax differs from taxing the pooled income.
    joint = tax_on_income(sum(incomes.values()), BRACKETS)
    individual = sum(result[r]['tax_before'] for r in incomes)
    assert individual < joint


def test_private_loan_interest_adjusts_only_that_adults_taxable_income():
    """Issue #813: lender interest / borrower deduction land on the OWN slot."""
    cfg = _StubConfig(['primary', 'spouse'])
    incomes = {'primary': 60_000, 'spouse': 60_000}
    # Primary lends (accrues +5000 interest); spouse borrows for investment
    # (-5000 deductible). Each adjusts only their own taxable income.
    loans = {'primary': (5_000.0, 0.0), 'spouse': (0.0, 5_000.0)}

    result = _income_tax_by_adult(cfg, incomes, loans, BRACKETS)

    assert result['primary']['taxable_income'] == 65_000
    assert result['spouse']['taxable_income'] == 55_000
    assert result['primary']['tax_before'] == pytest.approx(
        _expected_tax(cfg, 65_000, 65_000, BRACKETS))
    assert result['spouse']['tax_before'] == pytest.approx(
        _expected_tax(cfg, 55_000, 55_000, BRACKETS))


def test_loop_generalizes_to_more_than_two_adults():
    """The loop taxes every adult config.adults() yields, not just two."""
    cfg = _StubConfig(['primary', 'spouse', 'grandparent'])
    incomes = {'primary': 120_000, 'spouse': 40_000, 'grandparent': 30_000}
    loans = {r: (0.0, 0.0) for r in incomes}

    result = _income_tax_by_adult(cfg, incomes, loans, BRACKETS)

    assert set(result) == {'primary', 'spouse', 'grandparent'}
    assert result['grandparent']['tax_before'] == pytest.approx(
        _expected_tax(cfg, 30_000, 30_000, BRACKETS))


def test_absent_spouse_is_backfilled_for_the_two_slot_signature():
    """simulate_year_pure keeps its primary/spouse signature this step: a
    household that declares no spouse still gets a spouse slot, computed from
    its zero-income inputs -- byte-identical to the pre-loop code."""
    cfg = _StubConfig(['primary'])  # config.adults() omits the spouse
    incomes = {'primary': 90_000, 'spouse': 0.0}
    loans = {'primary': (0.0, 0.0), 'spouse': (0.0, 0.0)}

    result = _income_tax_by_adult(cfg, incomes, loans, BRACKETS)

    assert 'spouse' in result
    assert result['spouse']['rate'] == marginal_rate(0.0, BRACKETS)
    assert result['spouse']['tax_before'] == pytest.approx(
        _expected_tax(cfg, 0.0, 0.0, BRACKETS))
