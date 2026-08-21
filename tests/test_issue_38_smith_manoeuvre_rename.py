#!/usr/bin/env python3
"""Tests for Issue #38: smith_manoeuvre → readvance_investment rename (DP#7).

Per DP#7, model the mechanism, not the branded product. rate_model parameter
names should describe the mechanism (readvanceable investment) rather than
the branded strategy (Smith Manoeuvre).

Tests verify:
- readvance_invest_monthly parameter works in amortization_schedule
- smith_invest_monthly alias is removed (DP#9): passing it raises TypeError

The rental-comparison half of this issue (CashDam.compare_with_readvance() /
compare_with_smith()) was deleted along with the rest of
countries/canada/rental.py in epic #603 Track C Phase 2 (DP#9): the whole
rental_properties.* input block -- and RentalProperty/RentalExpenses/CashDam,
the classes that would have parsed it -- had zero production callers
(#593's DEAD_ALLOWLIST). A feature that never ran is not a feature.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRateModelRename(unittest.TestCase):
    """Test that rate_model uses mechanism-based names (DP#7)."""

    def test_readvance_invest_monthly_parameter(self):
        """readvance_invest_monthly should be accepted as a parameter."""
        from countries.canada.rate_model import amortization_schedule, build_rate_path

        rate_path = build_rate_path("Test", 0.04, 25, 'variable', [0.04])
        # Verify the parameter is accepted without error
        schedule = amortization_schedule(
            principal=100000,
            rate_path=rate_path,
            projection_months=12,
            readvance_smith=False,
            readvance_invest_monthly=True,
        )
        self.assertIsInstance(schedule, list)
        self.assertGreater(len(schedule), 0)

    def test_smith_invest_monthly_alias_removed(self):
        """smith_invest_monthly alias must be removed (DP#9): callers must use
        readvance_invest_monthly."""
        from countries.canada.rate_model import amortization_schedule, build_rate_path

        rate_path = build_rate_path("Test", 0.04, 25, 'variable', [0.04])
        with self.assertRaises(TypeError):
            amortization_schedule(
                principal=100000,
                rate_path=rate_path,
                projection_months=12,
                readvance_smith=True,
                smith_invest_monthly=True,
            )


if __name__ == '__main__':
    unittest.main()