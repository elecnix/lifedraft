"""Issue #344: the capital gains inclusion rate is a FLAT one-half.

The engine modelled the Budget 2024 two-thirds upper tier as though it had been
enacted. It never was: it was a Ways and Means motion, deferred on 31 January
2025, and the government announced on 21 March 2025 that it WOULD BE CANCELLED.
No individual has ever paid it.

Measured before the fix (origin/main):

    capital_gains_inclusion_rate(400_000, 2023) -> 0.50
    capital_gains_inclusion_rate(400_000, 2024) -> 0.5625
    capital_gains_inclusion_rate(400_000, 2025) -> 0.5625
    capital_gains_inclusion_rate(400_000, 2026) -> 0.5625
    capital_gains_inclusion_rate(400_000, 2027) -> 0.5604   <- carried forward
    capital_gains_inclusion_rate(1_000_000, 2025) -> 0.625

The tiered arithmetic is deliberately KEPT -- it is the mechanism the
year-versioned data drives, and it becomes correct again the day a tier is
actually enacted. What changed is that no year carries one.
"""

import unittest

from countries.canada.income_type import capital_gains_inclusion_rate


class TheInclusionRateIsFlat(unittest.TestCase):
    def test_flat_half_for_every_modelled_year(self):
        for year in (2023, 2024, 2025, 2026):
            self.assertEqual(capital_gains_inclusion_rate(400_000, year), 0.50,
                             msg=f"{year} is not flat 50%")

    def test_a_projected_year_inherits_no_tier_either(self):
        """The provider carries the last row forward, so 2027 used to pick up
        the tier from 2026 without anybody writing it down."""
        for year in (2027, 2030):
            self.assertEqual(capital_gains_inclusion_rate(400_000, year), 0.50)

    def test_a_large_gain_is_still_flat(self):
        """$1M returned 0.625 -- the clearest statement of the error."""
        self.assertEqual(capital_gains_inclusion_rate(1_000_000, 2025), 0.50)

    def test_both_sides_of_the_old_threshold_are_flat(self):
        for gain in (0, 249_999, 250_000, 250_001, 1_000_000):
            self.assertEqual(capital_gains_inclusion_rate(gain, 2026), 0.50,
                             msg=f"gain {gain}")


class NoRowCarriesTheUnenactedTier(unittest.TestCase):
    def test_no_year_versioned_row_declares_an_upper_tier(self):
        """Belt-and-braces: the DATA carries no tier, not just the function."""
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        for year in provider.available_years("canada", "federal"):
            row = provider.get_year_data(year, "canada", "federal")
            self.assertEqual(row.capital_gains_upper_inclusion_rate, 0.0,
                             msg=f"{year} declares an upper tier")
            self.assertEqual(row.capital_gains_threshold, 0.0,
                             msg=f"{year} declares a threshold")
            self.assertEqual(row.capital_gains_inclusion_rate, 0.50,
                             msg=f"{year} is not flat 50%")


if __name__ == "__main__":
    unittest.main()
