"""Issue #701 (Step 5 of #643): tax each adult individually via a loop.

The two-role taxable-income/tax block in ``simulation.py`` now iterates
``config.adults()`` (``_income_tax_by_adult``) instead of a hardcoded
primary/spouse pair. This step is purely ISOMORPHIC for the two-adult golden
household -- each adult is still taxed in their OWN bracket (Canada has no joint
filing), the loop just makes that explicit and generalizes to N adults. These
lean unit tests pin the invariants the loop must hold; the golden-trajectory
invariant (``test_golden_trajectory_581``) guards the end-to-end isomorphism.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simulation import _income_tax_by_adult  # noqa: E402
from tax_calculator import marginal_rate, tax_on_income  # noqa: E402


# A tiny stand-in for the config seam the helper depends on -- it only calls
# ``config.adults()`` (issue #699), so a stub returning role dicts is enough to
# exercise the loop without building a whole SimulationConfig.
class _StubConfig:
    def __init__(self, roles):
        self._roles = roles

    def adults(self):
        return [{'role': r} for r in self._roles]


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
        assert result[role]['tax_before'] == tax_on_income(income, BRACKETS)
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
    assert result['primary']['tax_before'] == tax_on_income(65_000, BRACKETS)
    assert result['spouse']['tax_before'] == tax_on_income(55_000, BRACKETS)


def test_loop_generalizes_to_more_than_two_adults():
    """The loop taxes every adult config.adults() yields, not just two."""
    cfg = _StubConfig(['primary', 'spouse', 'grandparent'])
    incomes = {'primary': 120_000, 'spouse': 40_000, 'grandparent': 30_000}
    loans = {r: (0.0, 0.0) for r in incomes}

    result = _income_tax_by_adult(cfg, incomes, loans, BRACKETS)

    assert set(result) == {'primary', 'spouse', 'grandparent'}
    assert result['grandparent']['tax_before'] == tax_on_income(30_000, BRACKETS)


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
    assert result['spouse']['tax_before'] == tax_on_income(0.0, BRACKETS)
