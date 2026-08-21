"""Long-horizon golden scenario + year-by-year trajectory invariants (issue #581, epic #603).

## The gap this closes

3,900+ passing tests caught none of #575-#580. They assert terminal scalars
over a ~10-year default horizon; every one of those bugs lives in the
*trajectory*, or in a regime (retirement, RRIF conversion at 71, RESP
wind-down, death) the default horizon never reaches.

This file provides the thin slice of #581 the epic calls the hard dependency
for the rest of the backlog: one long-horizon household, fabricated and
round-numbered (DP#4/DP#15 -- no personal data, ever), run for 46 years so it
crosses every regime -- accumulation -> retirement -> RRIF conversion at 71 ->
long decumulation toward end of life -- plus a battery of year-by-year
invariant checks (``tests/trajectory_invariants.py``) run against every
single projected year, not just the last.

## What's asserted, and what's xfailed

Invariants that hold on ``main`` today (regression guards -- these must stay
green):
  - no NaN/inf anywhere in the trajectory
  - no negative balances, no negative debt
  - mortgage amortization conserves principal (no money appears/vanishes)
  - ACB never exceeds FMV on the non-reg account (DP#19)
  - the RRIF minimum actually fires from age 71, at least the statutory
    age-factor x January-1 balance (PR #574's fix, independently re-verified
    here against the published CRA factor table via TaxDataProvider)
  - the retirement drawdown nets the target spend, every year, within a
    dollar (#363/#579: plan_drawdown_net is now the only drawdown model --
    the old blended-rate gross path over-drew and has been deleted)
  - **#577 (fixed)** -- an undrawn HELOC margin is no longer booked as debt;
    heloc_balance stays 0 for the whole 46-year horizon in this scenario,
    which never draws it (no lump_sum, no SM, heloc_readvance=False)
  - **#578 (fixed)** -- the RESP winds down as EAPs (taxed to the student)
    and PSE (contributions returned tax-free to the subscriber) across each
    beneficiary's study window, computed from birth_year + a configurable
    study start age/duration (DP#1/DP#28); any remainder after the window
    ends collapses via the AIP path (grant repayment + subscriber tax +
    20% penalty) instead of compounding forever

  - **#575 (fixed)** -- the non-reg balance grows by more than its own
    contributions, and its ACB diverges from FMV as unrealized gains accrue,
    instead of the account compounding at 0% whenever its own balance
    started at 0 (the ordinary case)
  - **#576 (fixed)** -- Smith-Manoeuvre investments carry tax drag; they are
    non-registered and taxable by construction (that is the whole basis of
    the s.20(1)(c) interest deduction), so they must not compound at the raw
    gross return (isolated fixture below, since the balance is not a field
    on ``YearResult``)

**Every invariant in this file now passes; nothing here is xfailed.** The
five bugs this file was built to catch -- #575, #576, #577, #578, and the
#363/#579 drawdown -- are all closed. That is the point of the instrument,
not a reason to retire it: these are regression guards now. A future change
that reintroduces any of them turns this file red, which is exactly what
3,900 terminal-scalar tests failed to do the first time around.

When adding a new known-broken invariant here, mark it
``xfail(strict=True)`` so that the day it is fixed the test flips green and
the stale marker becomes a hard CI failure -- an xfail must never be able to
outlive the bug it describes.

No numeric golden snapshot is committed anywhere in this file -- invariants
only, so a legitimate fix landing behind this test does not need to touch it
(the mission brief is explicit: a committed snapshot of expected numbers
would conflict with every fix landing behind this one).
"""

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig, ScenarioOverlay, apply_overlay, YearResult

from trajectory_invariants import assert_invariant, run_invariant


# ============================================================================
# The golden household -- fabricated, round numbers, role-based names
# (DP#4/DP#15). A couple, two children, employment income, a mortgage, an
# undrawn HELOC margin, and RRSP/TFSA/FHSA/RESP/non-registered accounts.
# ============================================================================

START_YEAR = 2026
PRIMARY_BIRTH = 1976   # age 50 at start; retires 2041 (age 65); RRIF conversion 2047 (age 71)
SPOUSE_BIRTH = 1978    # age 48 at start; retires 2043 (age 65)
CHILD_A_BIRTH = 2010   # age 16 at start; study window ends well before either RRIF or horizon end
CHILD_B_BIRTH = 2013   # age 13 at start
HORIZON_AGE = 95       # -> last simulated year 2071: 46-year horizon crosses every regime
GROSS_RETURN = 0.06
OPENING_MORTGAGE = 400_000
OPENING_MARGIN = 150_000  # undrawn HELOC room -- never drawn in this scenario (#577 target)


def golden_household_config() -> dict:
    """A couple + 2 children, long-horizon (age 95), every account type.

    Deliberately ``use_readvanceable=False`` at run time (see ``_run`` below)
    and ``property.heloc_readvance: False`` -- this household never draws its
    HELOC margin, which makes ``margin_available`` an unambiguous #577 probe:
    any debt recorded against it is *entirely* phantom.
    """
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': PRIMARY_BIRTH, 'gross_income': 120_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_200,
                 'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000,
                 'fhsa_room_accumulated': 8_000,
                 'rrsp_balance': 300_000, 'tfsa_balance': 40_000},
                {'role': 'spouse', 'birth_year': SPOUSE_BIRTH, 'gross_income': 80_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_000,
                 'rrsp_room_accumulated': 25_000, 'tfsa_room_accumulated': 20_000,
                 'rrsp_balance': 150_000, 'tfsa_balance': 30_000},
            ],
            'children': [
                {'name': 'child_a', 'birth_year': CHILD_A_BIRTH},
                {'name': 'child_b', 'birth_year': CHILD_B_BIRTH},
            ],
        },
        'accounts': {
            'resp_current_balance': 40_000,
            'rrsp_annual_max': 31_000,
        },
        'assumptions': {
            'start_year': START_YEAR,
            'horizon_age': HORIZON_AGE,
            'investment_return': GROSS_RETURN,
            'salary_growth': 0.02,
            'inflation': 0.02,
            'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 0, 'cost_basis': 0,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01},
                },
            },
        },
        'property': {
            'house_value': 800_000,
            'mortgage_balance': OPENING_MORTGAGE,
            'mortgage_rate': 0.05,
            'amortization_years': 20,
            'margin_available': OPENING_MARGIN,
            'ltv_max': 0.80,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.20},
        'retirement': {
            'spending_target': 70_000,
            'rrif_conversion_age': 71,
        },
        'tax': {'province': 'qc'},
    }


def _run(cfg: dict, use_readvanceable: bool = False):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=use_readvanceable, deduct_later=False)
    return sim.run()


@pytest.fixture(scope='module')
def golden_results():
    return _run(golden_household_config())


@pytest.fixture(scope='module')
def golden_ctx():
    return {
        'start_year': START_YEAR,
        'primary_birth_year': PRIMARY_BIRTH,
        'spouse_birth_year': SPOUSE_BIRTH,
        'children_birth_years': [CHILD_A_BIRTH, CHILD_B_BIRTH],
        'gross_return': GROSS_RETURN,
        'opening_mortgage_balance': OPENING_MORTGAGE,
        'margin_never_drawn': True,
    }


def test_golden_scenario_covers_every_regime(golden_results):
    """Sanity check on the fixture itself, before trusting any invariant
    run against it: the horizon actually reaches accumulation, retirement,
    RRIF conversion, and long decumulation."""
    assert len(golden_results) == HORIZON_AGE - (START_YEAR - PRIMARY_BIRTH) + 1
    ages = [(START_YEAR + i) - PRIMARY_BIRTH for i in range(len(golden_results))]
    assert ages[0] < 65 < max(ages)            # starts pre-retirement, horizon outlives it
    assert any(age >= 71 for age in ages)       # reaches RRIF conversion
    assert ages[-1] == HORIZON_AGE
    assert golden_results[-1].employment_income == 0   # retired by the end
    assert any(r.drawdown_taxable > 0 for r in golden_results)  # RRIF minimum fired somewhere


# ============================================================================
# Invariants that hold on main today -- regression guards.
# ============================================================================

def test_no_nan_or_inf(golden_results, golden_ctx):
    assert_invariant('no_nan_or_inf', golden_results, golden_ctx)


def test_no_negative_balances(golden_results, golden_ctx):
    assert_invariant('no_negative_balances', golden_results, golden_ctx)


def test_debt_never_negative(golden_results, golden_ctx):
    assert_invariant('debt_never_negative', golden_results, golden_ctx)


def test_mortgage_amortization_conserves_principal(golden_results, golden_ctx):
    assert_invariant('mortgage_conserves_principal', golden_results, golden_ctx)


def test_acb_never_exceeds_fmv(golden_results, golden_ctx):
    assert_invariant('acb_le_fmv', golden_results, golden_ctx)


def test_rrif_minimum_fires_from_71(golden_results, golden_ctx):
    assert_invariant('rrif_minimum_fires_from_71', golden_results, golden_ctx)


def test_drawdown_meets_net_target(golden_results, golden_ctx):
    assert_invariant('drawdown_meets_net_target', golden_results, golden_ctx)


def test_resp_winds_down(golden_results, golden_ctx):
    assert_invariant('resp_winds_down_after_children_age_out', golden_results, golden_ctx)


# ============================================================================
# Formerly xfailed, now regression guards: #575 and #576 are fixed, and their
# xfail markers are gone. A new known-broken invariant added here should be
# marked xfail(strict=True), so that the day it is fixed this test flips to a
# hard failure ("test passed unexpectedly") until the marker is removed --
# a stale xfail cannot hide silently.
# ============================================================================

def test_non_reg_grows_beyond_contributions(golden_results, golden_ctx):
    """#575 (fixed): non-reg after-tax return is a pure function of portfolio
    composition and the marginal rate, never of the account's own current
    balance -- a $0 starting balance no longer means a 0% rate for the life
    of the projection."""
    assert_invariant('non_reg_grows_with_positive_return', golden_results, golden_ctx)


def test_acb_does_not_stay_pinned_to_fmv(golden_results, golden_ctx):
    """#575 (fixed): once the non-reg balance actually earns a return, ACB
    (contributions only) diverges from FMV (contributions + accrued growth)
    as expected, instead of tracking it 1:1 for the whole horizon."""
    assert_invariant('acb_not_pinned_to_fmv', golden_results, golden_ctx)


def test_undrawn_heloc_margin_is_not_debt(golden_results, golden_ctx):
    """#577 (fixed): undrawn HELOC margin_available is no longer booked as
    heloc_balance debt. SimState.initial() leaves heloc_balance at 0;
    FamilySimulation only books a draw when the caller actually invests the
    margin via lump_sum (not exercised by this scenario:
    use_readvanceable=False, no personal draws, heloc_readvance=False, no
    lump_sum), so heloc_balance stays 0 for the full 46-year horizon."""
    assert_invariant('undrawn_heloc_margin_not_booked_as_debt', golden_results, golden_ctx)


# ============================================================================
# #576: Smith-Manoeuvre investment tax drag.
#
# `sm_investment_balance` is not a field on YearResult -- it is folded into
# `total_assets` (simulation_state.py) alongside FHSA and LIRA/LIF, which
# also are not individually exposed. To isolate it exactly, this fixture
# deliberately carries NO FHSA/LIRA/LIF, so
#   total_assets - (rrsp + tfsa + resp + non_reg + lira + lif)
# is exactly the SM investment balance for every year. Fabricated round
# numbers, role-based names (DP#4/DP#15) -- a separate small household from
# the main golden scenario, kept minimal on purpose.
# ============================================================================

SM_START_YEAR = 2026
SM_PRIMARY_BIRTH = 1980
SM_SPOUSE_BIRTH = 1982
SM_GROSS_RETURN = 0.06


def sm_only_config() -> dict:
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': SM_PRIMARY_BIRTH, 'gross_income': 150_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'birth_year': SM_SPOUSE_BIRTH, 'gross_income': 60_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 15_000, 'tfsa_room_accumulated': 20_000},
        ]},
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': SM_START_YEAR, 'projection_years': 20,
            'investment_return': SM_GROSS_RETURN, 'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': 600_000, 'mortgage_balance': 200_000, 'mortgage_rate': 0.05,
            'amortization_years': 10, 'margin_available': 200_000, 'ltv_max': 0.80,
            'heloc_readvance': True,
        },
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
    }


@pytest.fixture(scope='module')
def sm_results():
    return _run(sm_only_config(), use_readvanceable=True)


def test_sm_investment_has_tax_drag(sm_results):
    """#576 (fixed): SM investments now grow at the same after-tax rate as
    the plain non-reg account (declared yield taxed annually, capital
    appreciation deferred against cost basis) instead of the raw gross
    (tax-sheltered) rate -- they are non-registered/taxable by construction,
    which is the entire basis of the s.20(1)(c) interest deduction."""
    ctx = {
        'start_year': SM_START_YEAR,
        'gross_return': SM_GROSS_RETURN,
        'sm_investment_balances': [
            r.total_assets - (r.total_rrsp + r.total_tfsa + r.resp_balance
                               + r.non_reg_balance + r.lira_balance + r.lif_balance)
            for r in sm_results
        ],
    }
    assert_invariant('sm_investment_has_tax_drag', sm_results, ctx)


# ============================================================================
# #257/#577/#619: money conservation on a year-0 cash-out lump sum.
#
# A genuine LTV refinance -- cash_out is a mortgage increase whose proceeds,
# together with the pre-existing undrawn margin, are invested as a year-0
# lump sum via simulate.py's apply_overlay path (simulate.py:103:
# ``lump_sum = config.margin_available + overlay.cash_out``). The GridOptimizer
# / ScipyOptimizer path applies the identical rule through
# ``simulation_config.apply_ltv_overlay`` (#619: they used to each carry a
# private copy that inflated ``margin_available`` by ``cash_out`` -- deleted,
# DP#9). Fabricated round numbers, role-based names (DP#4/DP#15).
# ============================================================================

CASHOUT_START_YEAR = 2026
CASHOUT_PRIMARY_BIRTH = 1980
CASHOUT_SPOUSE_BIRTH = 1982
CASHOUT_GROSS_RETURN = 0.06
CASHOUT_OPENING_MORTGAGE = 200_000
CASHOUT_OPENING_MARGIN = 150_000   # undrawn HELOC room, pre-refinance
CASHOUT_HOUSE_VALUE = 600_000
CASHOUT_LTV = 0.70
# #257 rule: cash_out is the mortgage increase needed to reach the target LTV.
CASHOUT_CASH_OUT = CASHOUT_LTV * CASHOUT_HOUSE_VALUE - CASHOUT_OPENING_MORTGAGE  # 220,000


def cashout_base_config() -> dict:
    # DP#17/note on the invariant: RRSP room is deliberately 0 for both
    # members. A non-zero RRSP contribution in year 0 generates an RRSP
    # refund that simulate_year_pure applies straight to HELOC paydown
    # (see trajectory_invariants.check_invested_capital_equals_new_debt's
    # docstring) -- a second, legitimate same-year debt-reducing money flow
    # that would confound this fixture's job of isolating the cash-out draw.
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': CASHOUT_PRIMARY_BIRTH, 'gross_income': 150_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'birth_year': CASHOUT_SPOUSE_BIRTH, 'gross_income': 60_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
        ]},
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': CASHOUT_START_YEAR, 'projection_years': 10,
            'investment_return': CASHOUT_GROSS_RETURN, 'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': CASHOUT_HOUSE_VALUE, 'mortgage_balance': CASHOUT_OPENING_MORTGAGE,
            'mortgage_rate': 0.05, 'amortization_years': 20,
            'margin_available': CASHOUT_OPENING_MARGIN, 'ltv_max': 0.80,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
    }


@pytest.fixture(scope='module')
def cashout_results():
    """simulate.py's cash-out path end to end: apply_overlay sizes the
    mortgage increase and shrinks margin_available by the same amount (#664
    -- the mortgage and HELOC share ONE registered charge), then
    FamilySimulation invests the reduced margin + cash-out as the year-0
    lump sum (simulate.py:103's own formula). Here CASHOUT_CASH_OUT
    (220,000) EXCEEDS CASHOUT_OPENING_MARGIN (150,000), so the margin is
    fully consumed (floors at 0) and the year-0 draw is genuinely new debt,
    not a re-draw of the pre-existing HELOC room on top of it."""
    base_cfg = cashout_base_config()
    overlay = ScenarioOverlay(label='cashout_test', cash_out=CASHOUT_CASH_OUT,
                               mortgage_rate=base_cfg['property']['mortgage_rate'],
                               refinance_amortization_years=25)  # #655: fabricated round new-loan term
    overlaid_cfg = apply_overlay(base_cfg, overlay)
    sim_cfg = SimulationConfig.from_dict(overlaid_cfg)
    lump_sum = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False,
                            lump_sum=lump_sum)
    return sim.run()


def test_cashout_invests_exactly_what_was_borrowed(cashout_results):
    """#257/#577/#619/#664 (fixed): the year-0 lump sum equals the new debt
    actually recorded (mortgage increase + HELOC draw). Because
    CASHOUT_CASH_OUT (220,000) exceeds CASHOUT_OPENING_MARGIN (150,000),
    the pre-existing HELOC room is entirely consumed by the mortgage
    increase (#664: they share one charge) -- the new debt is exactly the
    cash-out, not margin + cash-out (that summed formula was #619's bug:
    treating the mortgage and HELOC as independent capacities).
    ``expected_year0_new_debt`` is computed straight from the fabricated
    household's raw numbers, not read back off ``apply_overlay``'s own
    output -- see the invariant's docstring for why that distinction is the
    entire point."""
    ctx = {
        'start_year': CASHOUT_START_YEAR,
        'opening_mortgage_balance': CASHOUT_OPENING_MORTGAGE,
        'opening_heloc_balance': 0.0,
        'expected_year0_new_debt': CASHOUT_CASH_OUT,
    }
    assert_invariant('invested_capital_equals_new_debt', cashout_results, ctx)


def test_invariant_would_have_caught_619():
    """Proof, not narration: replay the exact pre-#619 formula (margin_available
    inflated by cash_out inside the overlay, then lump_sum = cash_out +
    inflated_margin, capped-draw = min(lump_sum, inflated_margin)) and
    confirm this invariant flags it against the #664-correct expectation.
    #619's bug was self-consistent -- both optimizer copies agreed with each
    other, which is exactly why the cross-engine test that existed
    (test_apply_ltv_overlay_matches_grid_optimizer) missed it -- so a check
    that only compares engines to each other cannot be trusted here; this
    one compares against an independently-derived figure instead."""
    buggy_margin = CASHOUT_OPENING_MARGIN + CASHOUT_CASH_OUT          # the #619 bug
    buggy_lump_sum = CASHOUT_CASH_OUT + buggy_margin
    buggy_mortgage = CASHOUT_OPENING_MORTGAGE + CASHOUT_CASH_OUT
    buggy_heloc_draw = min(buggy_lump_sum, buggy_margin)               # margin_draw_for_lump_sum
    fake_year0 = YearResult(year=0, mortgage_balance=buggy_mortgage, heloc_balance=buggy_heloc_draw)

    ctx = {
        'start_year': CASHOUT_START_YEAR,
        'opening_mortgage_balance': CASHOUT_OPENING_MORTGAGE,
        'opening_heloc_balance': 0.0,
        'expected_year0_new_debt': CASHOUT_CASH_OUT,
    }
    violations = run_invariant('invested_capital_equals_new_debt', [fake_year0], ctx)
    assert len(violations) == 1
    # The fabricated invested capital overshoots by exactly the (already
    # cash-out-inflated, #619) buggy margin -- the #619/#664 double-count.
    assert violations[0].value - ctx['expected_year0_new_debt'] == pytest.approx(buggy_margin)
