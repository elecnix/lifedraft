"""Issue #346: a tax year before the first modelled schedule must be REFUSED.

## The bug

``TaxDataProvider._load_year_uncached`` resolved a year with no exact match to
the *nearest* available year. Forward of it, that projects the brackets
(DP#20) and is correct. Backward of it, it returned the nearest year's
``TaxYearData`` **unchanged, still labelled with that year's number** -- so
``get_year_data(2018, 'canada', 'quebec').year == 2023`` and the caller could
not tell. A contract dated 2018 is therefore simulated 2018-2022 on the 2023
tables: a plausible, confident, wrong number (DP#32).

## Why the refusal is its own exception type

The obvious fix -- ``raise ValueError(...)`` at the end of the resolver -- is
swallowed before it reaches anyone. Callers on the tax path catch
``ValueError`` to substitute a *different* fallback (a hardcoded DTC rate, a
round 15% lowest rate, the start-year brackets), so the refusal would turn
back into exactly the silent substitution #346 is about, one frame further
out. ``countries/canada/income_type.py`` has three such handlers; the three
optimizers that rank with ``except Exception: score = -inf`` are worse still.
``UnsupportedTaxYearError`` therefore derives from ``Exception`` and nothing
else, and the ``except Exception`` ranking blocks re-raise it explicitly --
the same narrow-catch contract ``optimizer.py`` already follows (DP#32, and
the ``except Exception: score = -inf`` entry in AGENTS.md's trap table).

## What is NOT asserted here

No pre-2023 schedule is being added. Modelling 2018-2022 needs sourced
figures per program per jurisdiction; this change only makes their absence
loud. The forward direction (DP#20 projection) must keep working -- refusing
2030 would break every multi-year projection, so
``TestForwardProjectionIsUnaffected`` pins it.
"""
from __future__ import annotations

import pytest

from tax_data import TaxDataProvider, TaxYearData, TaxBracket, UnsupportedTaxYearError

EARLIEST = 2023


@pytest.fixture
def provider():
    return TaxDataProvider()


# ── the bug itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("year", [2018, 2019, 2020, 2021, 2022])
def test_year_before_the_first_schedule_is_refused(provider, year):
    with pytest.raises(UnsupportedTaxYearError) as excinfo:
        provider.get_year_data(year, 'canada', 'quebec')
    assert str(year) in str(excinfo.value)


def test_refusal_is_actionable(provider):
    """The message must name the jurisdiction and the earliest year, so the
    reader knows what to change -- "before 2023", not just "no data"."""
    with pytest.raises(UnsupportedTaxYearError) as excinfo:
        provider.get_year_data(2018, 'canada', 'quebec')
    message = str(excinfo.value)
    assert 'canada/quebec' in message
    assert f'before {EARLIEST}' in message


def test_get_brackets_refuses_rather_than_returning_2023_brackets(provider):
    """``get_brackets`` was the other half of the issue's acceptance test."""
    with pytest.raises(UnsupportedTaxYearError):
        provider.get_brackets(2018, 'canada', 'quebec')


def test_get_brackets_never_hands_back_another_years_thresholds(provider):
    """The bug's own example: 2018's first federal bracket came back as
    $53,359 (the 2023 threshold), not CRA's $46,605. A refusal is the only
    correct answer until a sourced 2018 schedule exists."""
    with pytest.raises(UnsupportedTaxYearError):
        provider.get_brackets(2018, 'canada', 'federal')


def test_no_object_is_ever_returned_stamped_with_another_year(provider):
    """Acceptance: a result's ``.year`` must equal the requested year.

    Asserted over every jurisdiction that carries a schedule, not just
    Quebec, so adding a table for one province cannot re-open the hole for
    another.
    """
    for province in ('federal', 'quebec', 'ontario', 'alberta'):
        for year in range(2010, EARLIEST + 1):
            try:
                data = provider.get_year_data(year, 'canada', province)
            except (UnsupportedTaxYearError, ValueError):
                # A refusal of either kind is an acceptable answer. (Ontario
                # and Alberta carry no 2023 table at all, which already
                # raised ValueError before this issue was filed.)
                continue
            assert data.year == year, (
                f"{province}/{year} was answered with a {data.year} schedule"
            )


def test_refusal_repeats_from_the_memoized_path(provider):
    """``_load_year`` memoizes. Two callers, same answer -- a cache that
    stored the refusal as a value would let the second one through."""
    for _ in range(2):
        with pytest.raises(UnsupportedTaxYearError):
            provider.get_year_data(2018, 'canada', 'quebec')


# ── the refusal must not be one of the caught types ───────────────────────

@pytest.mark.parametrize("caught", [ValueError, AttributeError, KeyError,
                                    IndexError, LookupError])
def test_refusal_is_not_a_caught_tax_path_exception(caught):
    """Guards the mechanism of the fix, not just its outcome.

    The handlers that swallow this refusal are written as
    ``except (ValueError, AttributeError)`` and friends. If this type ever
    grew one of those bases, every one of them would start swallowing it
    again and the bug would return unannounced.
    """
    assert not issubclass(UnsupportedTaxYearError, caught)


def test_a_registered_historical_schedule_makes_the_year_answerable():
    """The refusal is about MISSING data, not a wall at 2023.

    Registering a sourced 2018 schedule is all it takes to make 2018
    answerable again -- the issue's "or return sourced 2018 figures" path.
    This is also the only honest way to reach
    ``TaxDataProvider.get_fhsa_limit``'s pre-2023 branch, which no real
    canada/federal table can reach now that earlier years are refused.
    """
    provider = TaxDataProvider()
    provider.register_year(TaxYearData(
        year=2018, country='canada', province='federal',
        federal_brackets=[TaxBracket(0, 46_605, 0.15, '15%')],
        basic_personal_amount=11_316, fhsa_limit=0,
    ))
    assert provider.get_year_data(2018, 'canada', 'federal').year == 2018
    # CRA's published 2018 first bracket, not the 2023 threshold of $53,359.
    assert provider.get_brackets(2018, 'canada', 'federal')[0].max_income == 46_605
    assert provider.get_fhsa_limit(2018) == 0


def test_dtc_income_paths_refuse_instead_of_substituting_hardcoded_rates():
    """``countries/canada/income_type.py`` catches (ValueError, AttributeError)
    around the provider and substitutes hardcoded 2023-era DTC rates. A
    pre-2023 year must reach the caller, not one of those literals."""
    from countries.canada import income_type

    with pytest.raises(UnsupportedTaxYearError):
        income_type._get_federal_dtc_rates(2018)
    with pytest.raises(UnsupportedTaxYearError):
        income_type._get_provincial_dtc_rates('quebec', 2018)
    with pytest.raises(UnsupportedTaxYearError):
        income_type.capital_gains_inclusion_rate(100_000, 2018)


def test_the_refusal_survives_the_income_tax_calculator():
    """``countries/canada/tax_calc.py`` catches (ValueError, IndexError) at
    four sites and substitutes a round 15% lowest rate. The federal tax
    function itself has no handler, so the whole income-tax path refuses."""
    from countries.canada import tax_calc

    with pytest.raises(UnsupportedTaxYearError):
        tax_calc.federal_tax(90_000, 2018, 'quebec')


# ── the forward direction must keep working (DP#20) ───────────────────────

@pytest.mark.parametrize("year", [2027, 2030, 2040])
def test_future_years_still_project_forward(provider, year):
    data = provider.get_year_data(year, 'canada', 'quebec')
    assert data.year == year
    assert data.source == 'projected'


def test_table_years_still_resolve_to_themselves(provider):
    for year in provider.available_years('canada', 'quebec'):
        data = provider.get_year_data(year, 'canada', 'quebec')
        assert data.year == year
        assert data.source == 'fallback'


# ── it has to reach the top, not just the provider ─────────────────────────

def test_a_back_dated_run_is_refused_by_the_fold():
    """Drives the fold, not the provider (DP#11/DP#18).

    A provider-level refusal proves nothing about the run: the whole point
    of #346 is that a dozen frames swallow exceptions on the way out. This
    runs the same long-horizon household the golden fixture runs, back-dated
    to 2018, and requires the refusal to arrive.
    """
    from test_golden_trajectory_581 import golden_household_config, _run

    cfg = golden_household_config()
    cfg['assumptions']['start_year'] = 2018
    with pytest.raises(UnsupportedTaxYearError) as excinfo:
        _run(cfg)
    assert f'before {EARLIEST}' in str(excinfo.value)


def _optimizer_config(start_year):
    """A small but complete household the optimizers can actually run."""
    from simulation import SimulationConfig

    return SimulationConfig(
        start_year=start_year, projection_years=3, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'gross_income': 50_000,
             'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[],
        mortgage_balance=100_000, mortgage_rate=0.05, house_value=400_000,
    )


def test_a_run_starting_before_the_upper_tier_year_gets_no_amt():
    """``apply_amt`` skips years below the 2024 capital-gains upper tier.

    Before #346 a unit-test caller that omitted ``calendar_year`` got the
    projection INDEX (0, 1, ...) as its calendar year, so this branch was
    reachable only by accident. An unsupplied calendar year now resolves to
    ``config.start_year + year``, which makes the branch reachable the way it
    was always meant to be: by a run that genuinely starts in 2023.
    """
    from simulation_config import SimulationConfig
    from simulation_state import SimState, _build_year_inputs, simulate_year_pure
    from canada_state_accessors import _default_canada_state

    config = SimulationConfig(
        start_year=2023, projection_years=1, investment_return=0.05,
        mortgage_balance=0, mortgage_rate=0.05, margin_available=0,
        province='quebec', children=[],
        family_members=[{'role': 'primary', 'gross_income': 400_000,
                          'birth_year': 1955}],
    )
    state = SimState(
        non_reg_balance=500_000, non_reg_acb=100_000,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    result, _ = simulate_year_pure(
        state=state,
        year=0,
        inputs=_build_year_inputs(
            allocations={'_primary_income': 400_000, '_annual_savings': 0},
            config=config, investment_return=0.05,
            primary_marginal_rate=0.30, retiree_marginal_rate=0.30,
            drawdown_net_target=200_000, drawdown_order=['non_reg'],
            any_retired=True, retirement_spending_target=200_000,
        ),
    )
    assert result.amt_net_charge == 0, "no AMT below the 2024 upper-tier year"


def test_monte_carlo_optimizer_does_not_rank_a_refusal_last():
    """``except Exception: score = -inf`` would turn the refusal back into a
    silently-ranked-last scenario (AGENTS.md's trap table; #657)."""
    from countries.canada.strategies import STRATEGY_BALANCED
    from monte_carlo_optimizer import MonteCarloOptimizer

    opt = MonteCarloOptimizer(_optimizer_config(2018), n_simulations=2,
                              seed_base=42)
    with pytest.raises(UnsupportedTaxYearError):
        opt.optimize(strategies=[STRATEGY_BALANCED])


def test_scipy_optimizer_does_not_rank_a_refusal_last():
    from countries.canada.strategies import STRATEGY_BALANCED
    from scipy_optimizer import ScipyOptimizer

    # 'rrsp_weight', not 'ltv': an ltv sweep with no declared
    # refinance_amortization_years refuses on its own before the fold runs,
    # which would test the wrong refusal.
    opt = ScipyOptimizer(_optimizer_config(2018), optimize_vars=['rrsp_weight'])
    with pytest.raises(UnsupportedTaxYearError):
        opt.optimize(strategies=[STRATEGY_BALANCED])


def test_dp_optimizer_does_not_rank_a_refusal_last():
    from countries.canada.strategies import STRATEGY_BALANCED
    from dp_optimizer import DPOptimizer
    from return_model import FixedReturn

    opt = DPOptimizer(_optimizer_config(2018), FixedReturn(0.07))
    with pytest.raises(UnsupportedTaxYearError):
        opt.optimize(strategies=[STRATEGY_BALANCED])


def test_deduct_later_dp_sweep_does_not_rank_a_refusal_last():
    """The other ``except Exception`` block in dp_optimizer.py, on the
    deduct_later claim-fraction sweep."""
    from countries.canada.strategies import STRATEGY_BALANCED
    from dp_optimizer import DPOptimizer, Decision, DECISION_DRAWDOWN_ORDER
    from return_model import FixedReturn

    opt = DPOptimizer(_optimizer_config(2018), FixedReturn(0.07))
    with pytest.raises(UnsupportedTaxYearError):
        opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=Decision(name='drawdown_order',
                                    decision_type=DECISION_DRAWDOWN_ORDER))
