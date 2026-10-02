#!/usr/bin/env python3
"""Regression tests for #416 — year-versioned CPP ceilings must survive projection.

Three defects, all of the same shape: a value that is correct for one year
reaching a year it was never sourced for.

A. ``TaxDataProvider._project_from_base`` built its ``TaxYearData`` field by
   field and omitted ``cpp_max_benefit_65`` and the whole OAS/GIS block, so a
   projected year was handed the dataclass default ``0.0`` and the getter
   could not tell "this program pays nothing" from "this field was never
   copied". ``get_cpp_max_benefit_65(2030)`` answered 14448 — the *2024*
   maximum — via the ``return 14448  # DP#13: 2024 fallback`` literal.

B. The Quebec and Ontario ``year_2025`` records declared
   ``cpp_max_pensionable=68500``, the 2024 ceiling, beside a correct 2025
   ``cpp2_max_pensionable=81200`` on the same row. Federal says 71300.

These tests drive the real provider and the real fold entry points; none of
them rebuilds engine state by hand.
"""

import unittest

from countries.canada.self_employed_contributions import (
    compute_cpp2_contribution,
)
from countries.canada import retirement
from tax_data import TaxDataProvider, TaxYearData

# The ceilings the projection must carry (issue #416, Bug A).
_CARRIED_LIMITS = (
    "cpp_max_benefit_65",
    "oas_annual_max",
    "oas_annual_max_75plus",
    "oas_clawback_threshold",
    "gis_max_single",
    "gis_max_coupled",
    "gis_income_exemption",
)

# The 2025 YMPE as CRA publishes it (issue #416, Bug B).
YMPE_2025 = 71300


class TestProjectionCarriesBenefitCeilings(unittest.TestCase):
    """Bug A: the DP#20 projection must not delete the ceilings it carries."""

    def setUp(self):
        self.provider = TaxDataProvider(auto_register=True)

    def test_projection_carries_every_benefit_ceiling(self):
        """Each carried field is projected from the base, not defaulted to 0.

        The property under test is *carry*, so a field the base does not
        source has nothing to assert: it must stay 0, not become a stale
        literal. ``oas_annual_max_75plus`` and the three GIS fields are in
        that position today (populated in zero registered records — they are
        sourced from CPP_OAS_BY_YEAR at call time), which is why this walks
        every registered record instead of naming a jurisdiction.
        """
        factor = 1.02 ** (2030 - 2026)
        carried = 0
        for key, base in self.provider._fallbacks.items():
            if base.year != 2026:
                continue
            projected = self.provider._project_from_base(base, 2030)
            for field in _CARRIED_LIMITS:
                base_value = getattr(base, field)
                self.assertEqual(
                    getattr(projected, field),
                    round(base_value * factor, 2) if base_value else 0,
                    f"{key}.{field} was not carried by the projection "
                    f"(base {base_value}, projected "
                    f"{getattr(projected, field)})",
                )
                if base_value > 0:
                    carried += 1
        self.assertGreaterEqual(
            carried, 1,
            "no registered 2026 record populates any carried ceiling, so "
            "this test would pass on a projection that drops all of them",
        )

    def test_projection_keeps_a_provincially_sourced_oas_maximum(self):
        """A provincial record that populates OAS keeps it, indexed.

        Quebec's records populate ``oas_annual_max``; the federal ones leave
        it at 0. Reading the same line off a Quebec base is what makes the
        omission visible — 8908 goes in and 0.0 came out.
        """
        base = self.provider._load_year_uncached(2026, "canada", "quebec")
        self.assertGreater(base.oas_annual_max, 0)
        projected = self.provider._project_from_base(base, 2030)
        self.assertAlmostEqual(
            projected.oas_annual_max,
            round(base.oas_annual_max * 1.02 ** 4, 2), places=2,
        )

    def test_max_cpp_benefit_beyond_the_last_schedule_is_not_a_2024_literal(self):
        """get_cpp_max_benefit_65(2030) must not answer with the 2024 maximum."""
        value = self.provider.get_cpp_max_benefit_65(2030)
        self.assertNotEqual(
            value, 14448,
            "2030 resolved to 14448, the hardcoded 2024 fallback at "
            "tax_data.py: the projection dropped cpp_max_benefit_65",
        )
        self.assertGreater(value, 18092)  # escalated above the 2026 value

    def test_provider_and_retirement_agree_beyond_the_last_schedule(self):
        """Two live values for one year is the defect; they must converge.

        ``retirement.get_cpp_max_benefit_65`` reads CPP_OAS_BY_YEAR (ends at
        2026) and falls through to the provider. ``cpp_estimator`` reads the
        table directly. For a projected year the provider is the only
        year-aware path, so it must answer with the indexed ceiling.
        """
        self.assertAlmostEqual(
            retirement.get_cpp_max_benefit_65(2030),
            self.provider.get_cpp_max_benefit_65(2030),
            places=2,
        )

    def test_every_carried_limit_is_indexed_not_flat(self):
        """A projected ceiling must grow, not repeat the base year forever.

        Flat would be the same silent substitution wearing a different hat:
        correct for 2026, wrong for 2035.
        """
        base = self.provider._load_year_uncached(2026, "canada", "federal")
        for field in _CARRIED_LIMITS:
            base_value = getattr(base, field)
            if base_value <= 0:
                continue  # the federal record does not source this program
            far = getattr(self.provider._project_from_base(base, 2050), field)
            self.assertGreater(
                far, base_value,
                f"{field} projected flat from 2026 to 2050",
            )


class TestProvincialYMPE2025(unittest.TestCase):
    """Bug B: the 2025 Quebec and Ontario tables carry the 2024 YMPE."""

    def setUp(self):
        self.provider = TaxDataProvider(auto_register=True)

    def test_provincial_2025_ympe_matches_federal(self):
        for province in ("quebec", "ontario", "qc", "on"):
            data = self.provider._load_year_uncached(2025, "canada", province)
            self.assertEqual(
                data.cpp_max_pensionable, YMPE_2025,
                f"{province} 2025 declares YMPE "
                f"{data.cpp_max_pensionable}, not the CRA's {YMPE_2025}",
            )

    def test_2025_ympe_never_trails_the_2024_ympe(self):
        """The YMPE is non-decreasing by construction (DP#1: real dates).

        Ontario registers only 2025 and 2026, so it is compared against
        the federal 2024 record instead of a provincial one that does not
        exist. Quebec has all four years and is checked against its own.
        """
        federal_2024 = self.provider._load_year_uncached(2024, "canada", "federal")
        quebec_2024 = self.provider._load_year_uncached(2024, "canada", "quebec")
        for province, y2024 in (("ontario", federal_2024),
                                ("quebec", quebec_2024)):
            y2025 = self.provider._load_year_uncached(2025, "canada", province)
            self.assertGreater(
                y2025.cpp_max_pensionable, y2024.cpp_max_pensionable,
                f"{province}: the 2025 ceiling did not rise above 2024",
            )

    def test_contribution_base_for_2025_is_the_cra_ceiling(self):
        """The wrong ceiling reaches the fold through self_employed_contributions."""
        for province in ("quebec", "ontario"):
            result = compute_cpp2_contribution(
                90_000, year=2025, province=province,
            )
            self.assertEqual(
                result["ympe"], YMPE_2025,
                f"{province}: 2025 contribution computed on a "
                f"{result['ympe']} base",
            )
            self.assertEqual(result["year"], 2025)

    def test_2025_second_ceiling_is_untouched(self):
        """The neighbouring 2025 YAMPE was already right; keep it that way."""
        for province in ("quebec", "ontario"):
            data = self.provider._load_year_uncached(2025, "canada", province)
            self.assertEqual(data.cpp2_max_pensionable, 81200)


class TestProjectionRoundTripsKnownYears(unittest.TestCase):
    """Guard the fix: projecting onto the base year must be a no-op.

    ``_project_from_base`` is now the single path for every non-exact year
    (see #407), so a projection that disturbs a known year would be a new
    silent substitution rather than a fixed one.
    """

    def test_projecting_onto_the_base_year_changes_nothing(self):
        provider = TaxDataProvider(auto_register=True)
        for province in ("federal", "quebec", "ontario"):
            base = provider._load_year_uncached(2026, "canada", province)
            projected = provider._project_from_base(base, 2026)
            self.assertEqual(projected.year, base.year)
            for field in _CARRIED_LIMITS:
                self.assertEqual(
                    getattr(projected, field), getattr(base, field),
                    f"{province}.{field} moved under a zero-year projection",
                )

    def test_source_marks_the_record_as_projected(self):
        provider = TaxDataProvider(auto_register=True)
        base = provider._load_year_uncached(2026, "canada", "federal")
        self.assertEqual(
            provider._project_from_base(base, 2030).source, "projected")


if __name__ == "__main__":
    unittest.main()
