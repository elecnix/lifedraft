"""Test issue #96: non-round marginal rate defaults in simulate_year_pure.

DP#13/26: The previous defaults (0.43 and 0.20) matched this household's exact
combined Quebec+federal marginal rates, creating a dangerous illusion of
correctness when callers forgot to provide their own rates. The fix replaces
0.43 with 0.40 (a round fallback that is clearly NOT a real rate) while
keeping 0.20 as it's already round.

Similarly, ird_penalty.py and hbp_rules.py had marginal_rate=0.43 defaults
that have been changed to 0.40.
"""

import unittest
import inspect
from simulation_state import simulate_year_pure
from countries.canada.ird_penalty import refinance_with_penalty_analysis, break_for_readvanceable_analysis
from countries.canada.hbp_rules import hbp_missed_repayment_tax_impact


class TestMarginalRateDefaultsAreRound(unittest.TestCase):
    """Verify that marginal rate defaults are round numbers, not household-specific."""

    def test_simulate_year_pure_primary_marginal_rate_default_is_round(self):
        """DP#13/26: primary_marginal_rate default must be a round number (0.40),
        not the household-specific 0.43 that was previously hardcoded."""
        sig = inspect.signature(simulate_year_pure)
        default = sig.parameters['primary_marginal_rate'].default
        # Round defaults are obviously fake, preventing silent misuse
        self.assertEqual(default, 0.40,
                         f"primary_marginal_rate default should be 0.40 (round), got {default}")

    def test_simulate_year_pure_spouse_marginal_rate_default_is_round(self):
        """spouse_marginal_rate default is already 0.20 (round), verify it stays round."""
        sig = inspect.signature(simulate_year_pure)
        default = sig.parameters['spouse_marginal_rate'].default
        self.assertEqual(default, 0.20,
                         f"spouse_marginal_rate default should be 0.20 (round), got {default}")

    def test_ird_penalty_refinance_default_is_round(self):
        """DP#13/26: marginal_rate default in refinance_with_penalty_analysis
        must be 0.40, not the household-specific 0.43."""
        sig = inspect.signature(refinance_with_penalty_analysis)
        default = sig.parameters['marginal_rate'].default
        self.assertEqual(default, 0.40,
                         f"refinance_with_penalty_analysis marginal_rate default should be 0.40, got {default}")

    def test_ird_penalty_readvanceable_default_is_round(self):
        """DP#13/26: marginal_rate default in break_for_readvanceable_analysis
        must be 0.40, not the household-specific 0.43."""
        sig = inspect.signature(break_for_readvanceable_analysis)
        default = sig.parameters['marginal_rate'].default
        self.assertEqual(default, 0.40,
                         f"break_for_readvanceable_analysis marginal_rate default should be 0.40, got {default}")

    def test_hbp_missed_repayment_default_is_round(self):
        """DP#13/26: marginal_rate default in hbp_missed_repayment_tax_impact
        must be 0.40, not the household-specific 0.43."""
        sig = inspect.signature(hbp_missed_repayment_tax_impact)
        default = sig.parameters['marginal_rate'].default
        self.assertEqual(default, 0.40,
                         f"hbp_missed_repayment_tax_impact marginal_rate default should be 0.40, got {default}")

    def test_no_043_default_in_simulation_state(self):
        """Verify no function in simulation_state.py has 0.43 as a default parameter."""
        with open('simulation_state.py', 'r') as f:
            content = f.read()
        # Search for =0.43 or = 0.43 in function signatures
        self.assertNotIn('=0.43', content.replace('= 0.43', ''),
                         "Found 0.43 default in simulation_state.py")
        self.assertNotIn('= 0.43', content,
                         "Found 0.43 default in simulation_state.py")

    def test_no_043_default_in_ird_penalty(self):
        """Verify no function in ird_penalty.py has 0.43 as a default parameter."""
        with open('countries/canada/ird_penalty.py', 'r') as f:
            content = f.read()
        self.assertNotIn('=0.43', content.replace('= 0.43', ''),
                         "Found 0.43 default in ird_penalty.py")
        self.assertNotIn('= 0.43', content,
                         "Found 0.43 default in ird_penalty.py")

    def test_no_043_default_in_hbp_rules(self):
        """Verify no function in hbp_rules.py has 0.43 as a default parameter."""
        with open('countries/canada/hbp_rules.py', 'r') as f:
            content = f.read()
        self.assertNotIn('=0.43', content.replace('= 0.43', ''),
                         "Found 0.43 default in hbp_rules.py")
        self.assertNotIn('= 0.43', content,
                         "Found 0.43 default in hbp_rules.py")


if __name__ == '__main__':
    unittest.main()