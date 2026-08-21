"""Issue #634 — collapse seven byte-identical year-versioned retirement getters.

The seven getters in countries/canada/retirement.py were pairwise 1.00-similarity
clones (dupdelta, the repo's clone detector). They must delegate to a single parameterized
implementation (DP#9) without any change in behaviour.

These tests are invariant-based:
  - RED: `_year_versioned_lookup` shared helper must exist and the getters must
    route through it.
  - Behaviour preservation: every getter returns exactly the CPP_OAS_BY_YEAR
    value for known years and falls back to the TaxDataProvider for unknown years.
"""
import pytest

import countries.canada.retirement as retirement


# (getter name, CPP_OAS_BY_YEAR key)
GETTERS = [
    ("get_oas_annual_max", "oas_annual_max"),
    ("get_oas_annual_max_75plus", "oas_annual_max_75plus"),
    ("get_oas_clawback_threshold", "oas_clawback_threshold"),
    ("get_gis_max_single", "gis_max_single"),
    ("get_gis_max_coupled", "gis_max_coupled"),
    ("get_cpp_max_pensionable", "cpp_max_pensionable"),
    ("get_cpp_max_benefit_65", "cpp_max_benefit_65"),
]

KNOWN_YEARS = sorted(retirement.CPP_OAS_BY_YEAR.keys())


def test_shared_helper_exists():
    """DP#9: a single parameterized lookup replaces the seven clones."""
    assert hasattr(retirement, "_year_versioned_lookup")


@pytest.mark.parametrize("name,key", GETTERS)
def test_getter_delegates_to_shared_helper(name, key, monkeypatch):
    """Each getter must route through _year_versioned_lookup (DP#9)."""
    calls = []
    real = retirement._year_versioned_lookup

    def spy(year, fallback_key, *args, **kwargs):
        calls.append((year, fallback_key))
        return real(year, fallback_key, *args, **kwargs)

    monkeypatch.setattr(retirement, "_year_versioned_lookup", spy)
    getattr(retirement, name)(2026)
    assert calls, f"{name} did not call _year_versioned_lookup"
    assert calls[0][1] == key


@pytest.mark.parametrize("name,key", GETTERS)
@pytest.mark.parametrize("year", KNOWN_YEARS)
def test_known_year_matches_table(name, key, year):
    """Behaviour preserved: known years return the CPP_OAS_BY_YEAR value."""
    expected = retirement.CPP_OAS_BY_YEAR[year][key]
    assert getattr(retirement, name)(year) == expected


@pytest.mark.parametrize("name,key", GETTERS)
def test_unknown_year_uses_provider(name, key, monkeypatch):
    """Behaviour preserved: an unknown year falls back to the TaxDataProvider."""
    sentinel = 12345.0

    class FakeProvider:
        def __getattr__(self, _n):
            return lambda year: sentinel

    monkeypatch.setattr("tax_data.default_tax_provider", lambda: FakeProvider())
    unknown = max(KNOWN_YEARS) + 100
    assert getattr(retirement, name)(unknown) == sentinel


@pytest.mark.parametrize("name,key", GETTERS)
def test_unknown_year_raises_when_provider_fails(name, key, monkeypatch):
    """Behaviour preserved: standard ValueError when no source has the year."""
    class FakeProvider:
        def __getattr__(self, _n):
            def _boom(year):
                raise KeyError("no data")
            return _boom

    monkeypatch.setattr("tax_data.default_tax_provider", lambda: FakeProvider())
    unknown = max(KNOWN_YEARS) + 100
    with pytest.raises(ValueError, match=r"Update CPP_OAS_BY_YEAR or TaxDataProvider"):
        getattr(retirement, name)(unknown)
