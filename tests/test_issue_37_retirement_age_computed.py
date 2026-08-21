"""Test issue #37: RetirementState.age stored and incremented instead of computed from birth_year.

DP#1: Age should be derived from birth_year and simulation year, not stored
as a mutable counter. This prevents staleness bugs where age becomes wrong
if the simulation year jumps or resets.

Key thresholds that depend on age:
- CPP start age (60/65/70)
- RRIF conversion age (71)
- OAS eligibility (65)
- Pension splitting eligibility (55/65)
"""

import unittest
from countries.canada.retirement import RetirementState, project_retirement


class TestRetirementStateBirthYear(unittest.TestCase):
    """DP#1: RetirementState should compute age from birth_year."""

    def test_birth_year_defaults_to_none(self):
        """birth_year defaults to None for backward compatibility."""
        state = RetirementState(rrif_balance=500000)
        self.assertIsNone(state.birth_year)

    def test_age_in_computes_from_birth_year(self):
        """age_in(year) computes age from birth_year, not stored age."""
        state = RetirementState(birth_year=1960, rrif_balance=500000)
        self.assertEqual(state.age_in(2025), 65)
        self.assertEqual(state.age_in(2030), 70)
        self.assertEqual(state.age_in(2035), 75)

    def test_age_in_different_years(self):
        """age_in correctly computes age for any simulation year."""
        state = RetirementState(birth_year=1979, rrif_balance=500000)
        self.assertEqual(state.age_in(2026), 47)
        self.assertEqual(state.age_in(2030), 51)
        self.assertEqual(state.age_in(2044), 65)  # OAS eligibility

    def test_birth_year_prevents_staleness(self):
        """DP#1: With birth_year, age is always correct regardless of simulation path."""
        state = RetirementState(birth_year=1960, rrif_balance=500000)
        # Age at 2025 is always 65, no matter how we got there
        self.assertEqual(state.age_in(2025), 65)
        # Even if we simulate non-sequentially
        self.assertEqual(state.age_in(2030), 70)
        self.assertEqual(state.age_in(2025), 65)  # Still correct

    def test_stored_age_without_birth_year(self):
        """Without birth_year, age falls back to stored value (backward compat)."""
        state = RetirementState(age=67, rrif_balance=500000)
        self.assertEqual(state.age, 67)
        # age_in without birth_year falls back to stored age
        self.assertEqual(state.age_in(2026), 67)

    def test_project_retirement_with_birth_year(self):
        """project_retirement uses birth_year to compute age."""
        state = RetirementState(
            birth_year=1958,  # Age 65 in 2023, 67 in 2025
            age=65,  # Initial age (will be overridden by birth_year computation)
            rrif_balance=500000,
            tfsa_balance=100000,
            non_reg_balance=200000,
            annual_expenses=40000,
            oas_annual=8000,
        )
        results = project_retirement(state, years=5, investment_return=0.05)
        # Results should have ages computed from birth_year
        self.assertEqual(len(results), 5)
        # First year: age computed from birth_year
        first_age = results[0]['age']
        self.assertGreaterEqual(first_age, 65)

    def test_birth_year_propagates_in_project_retirement(self):
        """birth_year is propagated from initial_state to internal state."""
        initial = RetirementState(
            birth_year=1958,
            age=65,
            rrif_balance=500000,
        )
        results = project_retirement(initial, years=3, investment_return=0.05)
        # Ages should increase by 1 each year
        ages = [r['age'] for r in results]
        self.assertEqual(ages[1], ages[0] + 1)
        self.assertEqual(ages[2], ages[1] + 1)

    def test_cpp_start_age_threshold(self):
        """CPP eligibility depends on computed age, not stored age."""
        # Born 1958: age 65 in 2023, age 70 in 2028
        state = RetirementState(
            birth_year=1958,
            cpp_start_age=65,
            rrif_balance=500000,
        )
        self.assertEqual(state.age_in(2023), 65)  # CPP eligible
        self.assertEqual(state.age_in(2028), 70)  # CPP deferred max

    def test_oas_eligibility_threshold(self):
        """OAS eligibility at age 65 uses computed age."""
        state = RetirementState(birth_year=1960, rrif_balance=500000)
        self.assertEqual(state.age_in(2025), 65)  # OAS eligible
        self.assertEqual(state.age_in(2024), 64)  # Not yet eligible

    def test_rrif_conversion_threshold(self):
        """RRIF conversion at age 71 uses computed age."""
        state = RetirementState(birth_year=1960, rrif_balance=500000)
        self.assertEqual(state.age_in(2031), 71)  # RRIF conversion year


class TestRetirementStateBackwardCompat(unittest.TestCase):
    """Ensure backward compatibility when birth_year is not set."""

    def test_age_field_still_works(self):
        """age field works when birth_year is not set."""
        state = RetirementState(age=70, rrif_balance=500000)
        self.assertEqual(state.age, 70)

    def test_project_retirement_without_birth_year(self):
        """project_retirement works with just age (backward compat)."""
        state = RetirementState(
            age=65,
            rrif_balance=500000,
            tfsa_balance=100000,
            annual_expenses=40000,
        )
        results = project_retirement(state, years=3, investment_return=0.05)
        self.assertEqual(len(results), 3)
        # Ages should still increase by 1
        ages = [r['age'] for r in results]
        self.assertEqual(ages[0], 65)
        self.assertEqual(ages[1], 66)
        self.assertEqual(ages[2], 67)


if __name__ == '__main__':
    unittest.main()