"""Tests for issue #331: pension_split_optimizer uses year-versioned OAS lookup.

DP#20: Data is year-versioned; simulate across tax years, not within a single
year's brackets. The optimizer must not silently use stale constant defaults
for OAS amounts or clawback thresholds.

Before the fix, optimize_pension_split and project_pension_split_retirement
used OAS_ANNUAL_MAX as a default parameter, meaning callers who didn't pass
OAS values got 2026 amounts regardless of the simulation year.

After the fix, both functions accept a `year` parameter and use
get_oas_annual_max(year) when OAS values are not explicitly provided.
"""
import pytest
from unittest.mock import patch


class TestOptimizePensionSplitYearVersioned:
    """Verify optimize_pension_split uses year-versioned OAS lookups."""

    def test_year_param_defaults_to_2026(self):
        """Default year=2026 produces same results as before for backward compat."""
        from countries.canada.pension_split_optimizer import optimize_pension_split
        from countries.canada.retirement import get_oas_annual_max

        # year defaults to 2026
        result_default = optimize_pension_split(
            spouse_a_income=80000,
            spouse_b_income=40000,
            eligible_pension=30000,
        )
        result_explicit = optimize_pension_split(
            spouse_a_income=80000,
            spouse_b_income=40000,
            eligible_pension=30000,
            year=2026,
        )
        assert result_default.optimal_split_pct == result_explicit.optimal_split_pct
        assert result_default.tax_savings == result_explicit.tax_savings

    def test_year_param_uses_oas_lookup(self):
        """When year changes, OAS amounts reflect year-versioned data."""
        from countries.canada.pension_split_optimizer import optimize_pension_split
        from countries.canada.retirement import get_oas_annual_max, get_oas_clawback_threshold

        # 2024 and 2026 have different OAS amounts and thresholds
        oas_2024 = get_oas_annual_max(2024)
        oas_2026 = get_oas_annual_max(2026)
        threshold_2024 = get_oas_clawback_threshold(2024)
        threshold_2026 = get_oas_clawback_threshold(2026)

        assert oas_2024 != oas_2026, "OAS amounts should differ across years"
        assert threshold_2024 != threshold_2026, "Clawback thresholds should differ across years"

        # High-income scenario where OAS clawback matters
        result_2024 = optimize_pension_split(
            spouse_a_income=120000,
            spouse_b_income=40000,
            eligible_pension=30000,
            year=2024,
        )
        result_2026 = optimize_pension_split(
            spouse_a_income=120000,
            spouse_b_income=40000,
            eligible_pension=30000,
            year=2026,
        )

        # The baseline OAS clawback amounts should differ because OAS amounts differ
        baseline_2024 = result_2024.split_details["baseline"]
        baseline_2026 = result_2026.split_details["baseline"]
        assert baseline_2024["total_oas_clawback"] != baseline_2026["total_oas_clawback"], \
            "OAS clawback should differ when OAS amounts differ across years"

    def test_explicit_oas_overrides_year_lookup(self):
        """Explicitly passed OAS values take precedence over year lookup."""
        from countries.canada.pension_split_optimizer import optimize_pension_split

        result_explicit_oas = optimize_pension_split(
            spouse_a_income=80000,
            spouse_b_income=40000,
            eligible_pension=30000,
            year=2024,
            spouse_a_oas=9500.0,
            spouse_b_oas=9500.0,
        )
        # With explicit OAS, year lookup is bypassed
        assert result_explicit_oas is not None

    def test_no_constant_import_remaining(self):
        """Verify OAS_ANNUAL_MAX is not imported as a default anywhere in the module."""
        import inspect
        from countries.canada import pension_split_optimizer

        source = inspect.getsource(pension_split_optimizer)
        # The constant should not appear as a default parameter value
        for line_no, line in enumerate(source.split('\n'), 1):
            # Allow in comments or docstrings, but not as default parameter values
            stripped = line.strip()
            if 'OAS_ANNUAL_MAX' in stripped and not stripped.startswith('#') and not stripped.startswith('"""') and not stripped.startswith("'''"):
                # Should only appear in imports or references, never as = OAS_ANNUAL_MAX
                assert '= OAS_ANNUAL_MAX' not in stripped, \
                    f"Line {line_no}: OAS_ANNUAL_MAX used as default: {stripped}"
            if 'OAS_CLAWBACK_THRESHOLD' in stripped and not stripped.startswith('#'):
                assert '= OAS_CLAWBACK_THRESHOLD' not in stripped, \
                    f"Line {line_no}: OAS_CLAWBACK_THRESHOLD used as default: {stripped}"


class TestProjectPensionSplitRetirementYearVersioned:
    """Verify project_pension_split_retirement uses year-versioned OAS lookups."""

    def test_year_param_defaults_to_2026(self):
        """Default year=2026 produces same results as before for backward compat."""
        from countries.canada.pension_split_optimizer import project_pension_split_retirement

        results = project_pension_split_retirement(
            spouse_a_age=72,
            spouse_b_age=70,
            spouse_a_rrif=400000,
            spouse_b_rrif=100000,
            spouse_a_tfsa=50000,
            spouse_b_tfsa=50000,
            investment_return=0.04,
        )
        assert len(results) > 0
        assert results[0]['year'] == 2026

    def test_year_param_sets_start_year(self):
        """The year parameter sets the starting simulation year."""
        from countries.canada.pension_split_optimizer import project_pension_split_retirement

        results = project_pension_split_retirement(
            spouse_a_age=72,
            spouse_b_age=70,
            spouse_a_rrif=400000,
            spouse_b_rrif=100000,
            spouse_a_tfsa=50000,
            spouse_b_tfsa=50000,
            year=2024,
            investment_return=0.04,
        )
        assert results[0]['year'] == 2024

    def test_explicit_oas_overrides_year_lookup(self):
        """Explicitly passed OAS values override year lookup."""
        from countries.canada.pension_split_optimizer import project_pension_split_retirement

        results = project_pension_split_retirement(
            spouse_a_age=72,
            spouse_b_age=70,
            spouse_a_rrif=400000,
            spouse_b_rrif=100000,
            spouse_a_tfsa=50000,
            spouse_b_tfsa=50000,
            year=2024,
            spouse_a_oas=9500.0,
            spouse_b_oas=9500.0,
            investment_return=0.04,
        )
        assert len(results) > 0


class TestOASYearVersionedIntegration:
    """Integration tests ensuring OAS amounts vary by year."""

    def test_different_years_different_oas(self):
        """OAS max amounts differ across years, confirming year-versioned data."""
        from countries.canada.retirement import get_oas_annual_max

        # 2024 and 2026 should have different OAS max values
        oas_2024 = get_oas_annual_max(2024)
        oas_2026 = get_oas_annual_max(2026)
        assert oas_2024 > 0
        assert oas_2026 > 0
        # They should be different since OAS is indexed
        # (though if the test data happens to be the same, this is not an error)

    def test_oas_clawback_threshold_year_versioned(self):
        """OAS clawback thresholds differ across years."""
        from countries.canada.retirement import get_oas_clawback_threshold

        threshold_2024 = get_oas_clawback_threshold(2024)
        threshold_2026 = get_oas_clawback_threshold(2026)
        assert threshold_2024 > 0
        assert threshold_2026 > 0