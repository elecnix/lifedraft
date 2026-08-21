#!/usr/bin/env python3
"""Issue #635: tax_bracket_fallbacks must not hand-mirror the canonical
bracket data (DP#9 -- two spellings of the same rule drift).

## What this guards

Pre-#635 ``build_fallback_data()`` hand-copied the federal/Quebec bracket
numbers from the canonical sources (``federal_all_years`` /
``QuebecTaxData``), and the copy had **already drifted**: the fallback's
federal 2026 record carried ``cpp_max_pensionable=71500`` (the 2025 YMPE)
and ``cpp_max_benefit_65=0`` while the canonical ``federal_all_years()[2026]``
had ``74600`` and ``18092``. Fallback mode would have served stale CPP
numbers with green tests in either mode.

The fallback is **load-bearing** (not a dead duplicate): it is called during
``countries.canada``'s re-entrant import, *before* the canonical
``federal_all_years`` in ``__init__`` is defined, so it cannot import that
name from ``countries.canada`` at call time. The fix derives it from a
province-independent module (``countries.canada.federal_tax_data``) that IS
importable mid-import, plus ``QuebecTaxData`` (also importable early). There
is no second spelling of any number to drift.

This test enforces the single-source-of-truth invariant: the fallback's
data MUST equal the canonical sources field-for-field. It fails on main
(proving the drift) and passes once the fallback is derived.

Fabricated round numbers only (DP#4/DP#15) -- this test asserts
*consistency between two sources*, not any real household's figures.
"""

import dataclasses
import unittest

import countries.canada  # noqa: F401 -- registers the fallback builder + canonical sources
from countries.canada import federal_all_years
from countries.canada.provinces.quebec.tax_data import QuebecTaxData
from countries.canada.tax_bracket_fallbacks import build_fallback_data


def _fallback(records, *, province, year=2026):
    return next(d for d in records
                if d.country == 'canada' and d.province == province and d.year == year)


class TestFallbackIsDerivedFromCanonical(unittest.TestCase):
    """The fallback data equals the canonical sources field-for-field (DP#9).
    Pre-#635 this failed: the hand-mirrored federal 2026 record had drifted
    (cpp_max_pensionable 71500 vs 74600, cpp_max_benefit_65 0 vs 18092)."""

    def test_federal_fallback_equals_canonical_federal_all_fields(self):
        records = build_fallback_data()
        fb_fed = _fallback(records, province='federal')
        canon_fed = next(y for y in federal_all_years() if y.year == 2026)
        # Field-for-field equality (TaxYearData is a dataclass with __eq__).
        # On main this fails on cpp_max_pensionable (71500 vs 74600) and
        # cpp_max_benefit_65 (0 vs 18092) -- the money shot.
        self.assertEqual(
            fb_fed, canon_fed,
            "the federal fallback must equal federal_all_years()[2026] field "
            "for field; pre-#635 it had drifted (cpp_max_pensionable 71500 vs "
            "74600, cpp_max_benefit_65 0 vs 18092).")

    def test_federal_fallback_cpp_matches_canonical(self):
        """The concrete drift the issue is about: CPP YMPE. On main the
        fallback served the stale 2025 YMPE (71500) for 2026; the canonical
        2026 value is 74600."""
        records = build_fallback_data()
        fb_fed = _fallback(records, province='federal')
        canon_fed = next(y for y in federal_all_years() if y.year == 2026)
        self.assertEqual(fb_fed.cpp_max_pensionable, canon_fed.cpp_max_pensionable)
        self.assertEqual(fb_fed.cpp_max_benefit_65, canon_fed.cpp_max_benefit_65)
        # Pin the 2026 value so a future regression to the stale 2025 figure
        # is caught by name, not just by relative comparison.
        self.assertEqual(fb_fed.cpp_max_pensionable, 74600)

    def test_quebec_fallback_provincial_data_equals_quebec_tax_data(self):
        """The Quebec fallback's provincial brackets come from
        QuebecTaxData.year_2026() (the single source), not a hand-copy."""
        records = build_fallback_data()
        fb_qc = _fallback(records, province='quebec')
        qc = QuebecTaxData.year_2026()
        self.assertEqual(fb_qc.provincial_brackets, qc.provincial_brackets)
        self.assertEqual(fb_qc.provincial_abatement, qc.provincial_abatement)
        self.assertEqual(fb_qc.basic_personal_amount, qc.basic_personal_amount)

    def test_quebec_fallback_federal_brackets_equal_canonical_federal(self):
        """The Quebec composite's federal brackets come from the canonical
        federal record (so get_brackets combines the same federal numbers
        whether in fallback or production mode)."""
        records = build_fallback_data()
        fb_qc = _fallback(records, province='quebec')
        canon_fed = next(y for y in federal_all_years() if y.year == 2026)
        self.assertEqual(fb_qc.federal_brackets, canon_fed.federal_brackets)

    def test_no_hand_copied_numbers_remain(self):
        """Sanity: the fallback module no longer carries hand-copied bracket
        literals -- it derives from the canonical sources, so its source has
        no TaxBracket(...) constructions of its own (the only numbers live in
        federal_tax_data / QuebecTaxData)."""
        import inspect
        from countries.canada import tax_bracket_fallbacks as tbf
        src = inspect.getsource(tbf)
        self.assertNotIn(
            'TaxBracket(', src,
            "tax_bracket_fallbacks must not construct TaxBracket literals -- "
            "it derives from federal_tax_data / QuebecTaxData (issue #635).")


class TestFallbackIsLoadBearing(unittest.TestCase):
    """The fallback is NOT a dead duplicate: it is called during
    countries.canada's re-entrant import (before the canonical
    federal_all_years in __init__ is defined). Documenting the finding so a
    future reader does not delete it expecting the canonical path to cover
    everything."""

    def test_fallback_registers_federal_and_quebec(self):
        records = build_fallback_data()
        provinces = {(d.province, d.year) for d in records}
        self.assertIn(('federal', 2026), provinces)
        self.assertIn(('quebec', 2026), provinces)

    def test_fallback_builder_is_registered_early(self):
        """The builder must be registered before the province imports so the
        re-entrant import can fall back to it (countries/canada/__init__.py
        registers it at line 34, before the province imports at line 36)."""
        import tax_data
        self.assertTrue(
            tax_data._FALLBACK_BUILDERS,
            "the fallback builder must be registered so the re-entrant "
            "import path (TaxDataProvider._build_hardcoded_fallbacks) has "
            "data before the canonical sources are defined (issue #635).")


if __name__ == '__main__':
    unittest.main()