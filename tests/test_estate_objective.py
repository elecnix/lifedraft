"""Tests for issue #580: wire the after-tax estate into the optimizer objective.

`objective.py`'s new `max_after_tax_estate` maps the terminal `YearResult`
onto `countries.canada.estate.compute_estate` (PR #574) so a strategy ranking
reflects what actually reaches the heirs -- an RRSP dollar taxed at death,
not counted the same as a tax-free TFSA dollar (DP#22, DP#32).

Uses the fabricated, round-number golden household from
`tests/test_golden_trajectory_581.py` (DP#4/DP#15 -- no personal data) so the
long-horizon (age 95) scenario reaches RRIF conversion and a realistic
terminal balance sheet without duplicating that fixture.
"""
import copy

import pytest

from objective import (
    MAX_AFTER_TAX_ESTATE, MAX_TERMINAL_WEALTH, MAX_NET_BENEFIT,
    compute_after_tax_estate, get_objective,
)
from year_result import YearResult
from trajectory_invariants import assert_invariant

from test_golden_trajectory_581 import golden_household_config, _run, START_YEAR


# ============================================================================
# Registration / plumbing (DP#22: the objective is data, selectable by name).
# ============================================================================

def test_max_after_tax_estate_is_registered():
    assert get_objective('max_after_tax_estate') is MAX_AFTER_TAX_ESTATE


def test_terminal_wealth_is_labelled_pre_tax_not_an_estate_value():
    """A pre-tax figure must never be labelled as if it were an estate value
    (mission #580, point 2). max_terminal_wealth's description must say so
    explicitly so nothing downstream can mistake it for the estate figure."""
    desc = MAX_TERMINAL_WEALTH.description.lower()
    assert 'pre-tax' in desc
    assert 'not an estate' in desc


# ============================================================================
# compute_after_tax_estate: unit-level wiring correctness.
# ============================================================================

def _yr(**kwargs) -> YearResult:
    """A single terminal YearResult with round, fabricated numbers (DP#4/#15)."""
    defaults = dict(
        primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_tfsa=0.0, non_reg_balance=0.0, non_reg_acb=0.0,
        lif_balance=0.0, lira_balance=0.0,
        mortgage_balance=0.0, heloc_balance=0.0, total_debt=0.0,
        total_assets=0.0,
    )
    defaults.update(kwargs)
    return YearResult(**defaults)


def test_registered_dollar_is_not_scored_like_a_tfsa_dollar():
    """The whole point of #580: $1 of RRSP must score less than $1 of TFSA
    once the objective is after-tax, even though both show up identically
    in total_assets."""
    cfg = {'tax': {'province': 'quebec', 'year': 2026}}

    rrsp_heavy = [_yr(primary_rrsp=600_000, total_assets=600_000)]
    tfsa_heavy = [_yr(total_tfsa=600_000, total_assets=600_000)]

    # Pre-tax: identical (this IS the bug the issue reports).
    assert MAX_TERMINAL_WEALTH.evaluate(rrsp_heavy, cfg) == \
        MAX_TERMINAL_WEALTH.evaluate(tfsa_heavy, cfg)

    # After-tax: the RRSP dollar is worth strictly less.
    rrsp_estate = MAX_AFTER_TAX_ESTATE.evaluate(rrsp_heavy, cfg)
    tfsa_estate = MAX_AFTER_TAX_ESTATE.evaluate(tfsa_heavy, cfg)
    assert rrsp_estate < tfsa_estate
    assert tfsa_estate == 600_000  # TFSA passes 100% tax-free
    assert rrsp_estate < 600_000   # RRSP is deemed-disposed at death


def test_house_and_non_mortgage_debt_are_not_double_counted():
    """A prior wiring pattern (retirement_analysis.py's estate_of()) nets the
    mortgage out of house_equity AND passes total_debt (which already
    includes the mortgage) as `debts` -- double-subtracting the mortgage.
    compute_after_tax_estate must net the mortgage exactly once.
    """
    cfg = {
        'property': {'house_value': 800_000},
        'tax': {'province': 'quebec', 'year': 2026},
    }
    # $200k mortgage still owing, no other debt.
    only_mortgage = [_yr(total_tfsa=100_000, total_assets=100_000,
                          mortgage_balance=200_000, total_debt=200_000)]
    est = compute_after_tax_estate(only_mortgage, cfg)
    # House FMV 800k - 200k mortgage = 600k net equity, all tax-free (TFSA
    # tax-free too) => gross estate should be 100k (tfsa) + 600k (house net)
    # = 700k, not 500k (which is what double-subtracting the mortgage would
    # give: 800k - 200k(house) - 200k(debts again) + 100k = 500k).
    assert est.gross_estate == pytest.approx(700_000)
    assert est.house_equity == pytest.approx(600_000)
    assert est.debts == pytest.approx(0.0)  # the mortgage is the ONLY debt


def test_estate_house_value_defaults_to_zero_when_absent_and_is_reported():
    """DP#13/#32: an absent `property.house_value` is a documented fallback
    for a missing *parameter*, not a coercion of a supplied value -- verify
    the fallback actually is 0 (not, say, a silently non-zero opinion)."""
    est = compute_after_tax_estate([_yr(total_tfsa=50_000, total_assets=50_000)], {})
    assert est.house_equity == 0.0
    assert est.net_estate == pytest.approx(50_000)


def test_empty_results_returns_zero_estate():
    from countries.canada.estate import EstateResult
    est = compute_after_tax_estate([], {})
    assert est == EstateResult()


# ============================================================================
# Trajectory invariant (mission #580, point 5): estate <= pre-tax balance
# sheet, every year, on the golden long-horizon household.
# ============================================================================

def test_estate_never_exceeds_pretax_balance_sheet_on_golden_scenario():
    cfg = golden_household_config()
    results = _run(cfg)
    ctx = {
        'start_year': START_YEAR,
        'house_value': cfg['property']['house_value'],
        'tax_province': cfg['tax']['province'],
    }
    assert_invariant('estate_value_never_exceeds_pretax_balance_sheet', results, ctx)


# ============================================================================
# Re-ranking: does the recommended strategy change once RRSP dollars are
# taxed? (mission #580 -- the question this issue exists to answer.)
#
# Reuses the golden household and the two named drawdown orders from
# retirement_analysis.py (TFSA-first vs RRSP-meltdown): same household, same
# spending, same horizon -- only the withdrawal order differs.
# ============================================================================

DRAWDOWN_ORDERS = {
    'tfsa_first': ['tfsa', 'non_reg', 'rrsp', 'lif', 'lira'],
    'rrsp_meltdown': ['rrsp', 'lif', 'lira', 'non_reg', 'tfsa'],
}


def _run_drawdown_strategy(order):
    cfg = copy.deepcopy(golden_household_config())
    cfg['retirement']['drawdown_order'] = order
    results = _run(cfg)
    obj_cfg = {
        'property': {'house_value': cfg['property']['house_value']},
        'tax': {'province': cfg['tax']['province'], 'start_year': START_YEAR},
    }
    return results, obj_cfg


def test_drawdown_ranking_can_flip_between_pretax_and_after_tax_objectives():
    """The pre-tax objective (max_terminal_wealth) cannot see the tax bomb
    embedded in a registered-heavy terminal balance; max_after_tax_estate
    can. This test doesn't hardcode which strategy wins -- it asserts the
    *mechanism*: the after-tax gap between strategies differs from the
    pre-tax gap once the deemed disposition is priced in, which is exactly
    what #580 says the old objective was blind to.
    """
    results_tfsa, cfg_tfsa = _run_drawdown_strategy(DRAWDOWN_ORDERS['tfsa_first'])
    results_melt, cfg_melt = _run_drawdown_strategy(DRAWDOWN_ORDERS['rrsp_meltdown'])

    pretax_tfsa = MAX_TERMINAL_WEALTH.evaluate(results_tfsa, cfg_tfsa)
    pretax_melt = MAX_TERMINAL_WEALTH.evaluate(results_melt, cfg_melt)
    estate_tfsa = MAX_AFTER_TAX_ESTATE.evaluate(results_tfsa, cfg_tfsa)
    estate_melt = MAX_AFTER_TAX_ESTATE.evaluate(results_melt, cfg_melt)

    # Both drawdown orders spend the same amount and hold the same total
    # financial assets under a pre-tax view (the accounts are just labeled
    # differently) -- the pre-tax objective must be far less sensitive to
    # drawdown order than the after-tax one.
    pretax_gap = abs(pretax_tfsa - pretax_melt)
    estate_gap = abs(estate_tfsa - estate_melt)
    assert estate_gap > pretax_gap, (
        f"after-tax gap ({estate_gap:,.0f}) should exceed the pre-tax gap "
        f"({pretax_gap:,.0f}) -- the deemed-disposition tax is exactly the "
        f"signal the pre-tax objective cannot see"
    )

    # RRSP-meltdown must always leave LESS registered balance exposed to the
    # deemed-disposition tax than TFSA-first (that is the entire mechanism).
    est_tfsa_full = compute_after_tax_estate(results_tfsa, cfg_tfsa)
    est_melt_full = compute_after_tax_estate(results_melt, cfg_melt)
    assert est_melt_full.registered_gross < est_tfsa_full.registered_gross
    assert est_melt_full.registered_tax < est_tfsa_full.registered_tax
