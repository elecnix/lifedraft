#!/usr/bin/env python3
"""Tax bracket fallback data module (DP#12 / DP#9).

Segregated data module that builds the fallback ``TaxYearData`` used by
``TaxDataProvider._build_hardcoded_fallbacks()``.

## Issue #635 — single source of truth (no hand-mirrored copy)

Pre-#635 this module **hand-mirrored** the bracket/limit numbers from the
canonical country/province modules
(``countries.canada.federal_all_years`` / ``QuebecTaxData``), and its own
docstring admitted the risk: *"Values here must be updated when the canonical
modules change."* That unenforced invariant had **already drifted**: the
fallback's federal 2026 record carried ``cpp_max_pensionable=71500`` (the
2025 YMPE) and ``cpp_max_benefit_65=0`` while the canonical
``federal_all_years()[2026]`` had ``74600`` and ``18092`` -- so fallback mode
would have served stale CPP numbers, with green tests in either mode.

The fallback now **derives** its data from the canonical sources at call
time -- there is no second spelling of any number to drift. The canonical
sources are imported LAZILY inside ``build_fallback_data()`` (not at module
top level): this module is imported by ``countries/canada/__init__.py``
*before* the province modules, so importing them at top level would re-enter
a half-initialized package. By call time (when
``_build_hardcoded_fallbacks`` runs, or a test calls this directly) the
``countries.canada`` package is fully imported, so ``federal_all_years`` and
``QuebecTaxData`` are available.

## When the fallback path actually runs

In the normal path ``TaxDataProvider.__init__`` succeeds at
``from countries import register_all`` and registers the real province
data; ``_build_hardcoded_fallbacks()`` is not called. The fallback path
runs only when a caller explicitly invokes ``_build_hardcoded_fallbacks()``
(the registered builder is itself populated by ``countries.canada``'s
import, so the canonical sources it now derives from are guaranteed
importable whenever the builder runs). See ``tax_data.py``'s
``_build_hardcoded_fallbacks`` and ``countries/canada/__init__.py``'s
``register_fallback_builder``.

Sources (canonical, the single source of truth):
  - countries.canada.federal_all_years (Federal brackets + federal limits)
  - countries.canada.provinces.quebec.tax_data.QuebecTaxData (Quebec brackets)
  - countries/canada.provinces.ontario.OntarioTaxData (Ontario brackets)

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md
"""

from dataclasses import replace

from tax_data import TaxYearData  # noqa: F401  -- re-exported for callers/tests


def build_fallback_data():
    """Build fallback ``TaxYearData`` DERIVED from the canonical sources.

    Returns a list of ``TaxYearData`` for the year/jurisdictions the
    fallback path serves. Every number comes from
    ``countries.canada.federal_all_years`` / ``QuebecTaxData`` -- there is
    no hand-copied value that can drift out of sync (issue #635, DP#9).

    The Quebec record is a COMPOSITE: Quebec's canonical provincial data
    with the canonical federal brackets overlaid, so ``get_brackets(...,
    combined=True)`` can combine federal+provincial from the one record
    (the shape the fallback path and ``test_issue_91`` expect). The federal
    record is the canonical federal ``TaxYearData`` verbatim.
    """
    # Lazy import: this builder is *called* during countries.canada's
    # re-entrant import (the province modules construct a TaxDataProvider
    # at import time, whose auto-registration falls back to
    # _build_hardcoded_fallbacks before countries.canada.__init__ is fully
    # defined). So we import from province-independent modules that are
    # importable at THAT moment:
    #   - federal_tax_data has only a tax_data dependency (no province deps)
    #     and is therefore importable mid-init (issue #635).
    #   - QuebecTaxData is importable early in quebec/__init__'s own import.
    # Importing from `countries.canada` directly would fail mid-init because
    # `federal_all_years` is not bound there until the package finishes.
    from countries.canada.federal_tax_data import federal_all_years
    from countries.canada.provinces.quebec.tax_data import QuebecTaxData

    fed_2026 = next(y for y in federal_all_years() if y.year == 2026)
    qc_2026 = QuebecTaxData.year_2026()
    # Composite Quebec record: Quebec's canonical provincial fields plus the
    # canonical federal brackets (so get_brackets combines from one record).
    # replace() keeps every Quebec field (provincial brackets, abatement,
    # BPA, QPP, OAS, Quebec credits, ...) untouched -- only federal_brackets
    # is added, sourced from the canonical federal record.
    qc_composite = replace(qc_2026, federal_brackets=fed_2026.federal_brackets)
    return [qc_composite, fed_2026]
