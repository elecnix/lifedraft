"""Tests for issue #1029: the optimizer OAS fallback tracks year-versioned data.

Before #1029, ``compute_net_benefit``'s fallback for ABSENT
``assumptions.oas_annual`` was the frozen literal ``8500`` (named by #986).
The year-versioned government table
``countries.canada.retirement.get_oas_annual_max(2026)`` returns **8908**, so
every household omitting ``assumptions.oas_annual`` was priced against a stale
benefit amount. After #1029 the fallback reads the live table for the
household's simulation start year (``cfg['tax']['start_year']``, which
``objective.objective_cfg`` always writes). Issue #290: a hand-built config
without that block now REFUSES instead of assuming a hardcoded current year.

Behaviour change by design: optimizer net-benefit numbers MOVE for households
omitting ``assumptions.oas_annual``. Households that DO declare it are
untouched (the dict.get default is never consulted). An explicit 0 stays 0
(DP#32 -- dict.get default, not ``x or DEFAULT``).

Tests drive ``compute_net_benefit`` itself (DP#11: the engine behaviour is
asserted through the engine entry point, not a reimplementation).
"""


def _final_year_result():
    """Issue #290: since the RRSP leg became the horizon deemed disposition,
    ``oas_annual`` reaches net benefit ONLY through the capital-gains leg's
    marginal rate (retirement income + taxable gain). A marginal RATE moves
    only when the income crosses a bracket edge, so the non-reg gain is sized
    from the live brackets (not a frozen number) to put the income halfway
    between ``edge - 8908`` and ``edge - 8500``: 0, 8500 and 8908 of OAS then
    land in different brackets. Retirement income here is CPP 14,400 (the
    fixture's 1,200/month) + LIF withdrawal 5,000."""
    from tax_data import default_tax_provider
    from year_result import YearResult
    edge = default_tax_provider().get_combined_brackets()[0]['max']
    income_without_oas = 14_400 + 5_000
    taxable_gain = edge - (8_500 + 8_908) / 2 - income_without_oas
    gain = taxable_gain / 0.50   # capital_gains_inclusion in _cfg
    non_reg_balance = 100_000
    return YearResult(
        year=2036,
        total_assets=500000,
        total_debt=200000,
        primary_rrsp=300000,
        total_rrsp=300000,
        total_tfsa=100000,
        non_reg_balance=non_reg_balance,
        non_reg_acb=non_reg_balance - gain,
        resp_balance=0,
        lif_withdrawal=5000,
        lif_balance=50000,
        lira_balance=0,
    )


def _cfg(oas_annual=None, start_year=2026, birth_year=1979):
    member = {'role': 'primary', 'gross_income': 130000,
              'cpp_monthly_estimated': 1200,
              'oas_start_age': 65,
              'pension_income_annual': 0}
    if birth_year is not None:
        member['birth_year'] = birth_year
    members = [member]
    assumptions = {'capital_gains_inclusion': 0.50}
    if oas_annual is not None:
        assumptions['oas_annual'] = oas_annual
    cfg = {'family': {'members': members}, 'assumptions': assumptions}
    if start_year is not None:
        cfg['tax'] = {'province': 'quebec', 'start_year': start_year}
    return cfg


class TestFallbackTracksYearVersionedTable:
    """The absent-input default equals get_oas_annual_max(relevant year)."""

    def test_default_is_8908_for_2026_not_the_stale_8500(self):
        from net_benefit_legs import _default_oas_annual
        assert _default_oas_annual({'tax': {'start_year': 2026}}) == 8908

    def test_absent_start_year_refuses_rather_than_assuming_one(self):
        """Issue #290: no ``tax.start_year`` -> a loud refusal, not 2026."""
        import pytest
        from net_benefit_legs import _default_oas_annual
        with pytest.raises(ValueError, match=r"cfg\['tax'\]\['start_year'\]"):
            _default_oas_annual({})

    def test_default_reads_table_for_cfg_start_year(self):
        from countries.canada.retirement import get_oas_annual_max
        from net_benefit_legs import _default_oas_annual
        cfg = {'tax': {'province': 'quebec', 'start_year': 2026}}
        assert _default_oas_annual(cfg) == get_oas_annual_max(2026)

    def test_engine_net_benefit_moves_for_household_omitting_oas_annual(self):
        """The intended correctness delta: absent oas_annual now prices OAS at
        the table value (8908), so net benefit differs from the pre-#1029
        frozen-8500 pricing. Since #290 ``oas_annual`` moves net benefit only
        through the capital-gains leg's retirement income."""
        from objective import compute_net_benefit
        results = [_final_year_result()]
        cfg_no_birth_year = _cfg(birth_year=None)
        net_absent = compute_net_benefit(results, cfg_no_birth_year)
        net_frozen_8500 = compute_net_benefit(
            results, _cfg(oas_annual=8500, birth_year=None))
        assert net_absent != net_frozen_8500

    def test_absent_matches_explicit_table_value(self):
        """A household omitting oas_annual is priced exactly as one that
        declares the table value -- the default IS the table read."""
        from countries.canada.retirement import get_oas_annual_max
        from objective import compute_net_benefit
        results = [_final_year_result()]
        net_absent = compute_net_benefit(results, _cfg(birth_year=None))
        net_declared = compute_net_benefit(
            results, _cfg(oas_annual=get_oas_annual_max(2026), birth_year=None))
        assert net_absent == net_declared


class TestDeclaredValuesUntouched:
    """Only ABSENT input takes the default; declared values pass through."""

    def test_declared_8500_still_honoured(self):
        from objective import compute_net_benefit
        results = [_final_year_result()]
        assert (compute_net_benefit(results, _cfg(oas_annual=8500))
                == compute_net_benefit(results, _cfg(oas_annual=8500.0)))

    def test_explicit_zero_stays_zero(self):
        """DP#32: an explicit oas_annual of 0 must not be coerced to 8908."""
        from objective import compute_net_benefit
        results = [_final_year_result()]
        net_zero = compute_net_benefit(results, _cfg(oas_annual=0, birth_year=None))
        net_table = compute_net_benefit(results, _cfg(birth_year=None))
        assert net_zero != net_table
