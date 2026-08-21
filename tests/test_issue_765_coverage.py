#!/usr/bin/env python3
"""Issue #765: cover the uncovered lines PR #757 (decumulation shortfall) left
behind -- tests-only, behaviour-preserving.

The per-file coverage gate (tools/coverage_gate.py) was merely tolerating
these via the baseline ratchet rather than the lines being exercised. This
file drives the specific uncovered lines in the three files #757 touched or
grew:

  * decumulation.py -- the shortfall_of() return (the helper
    worst_drawdown_shortfall() routes every row through).
  * optimize.py -- the _print_decumulation_shortfall_report() console
    deliverable #757 added (its "not engaged" early-return and its
    "render the registered caveat" branch), plus compute_net_benefit()'s
    no-birth_year retirement-income path (the retirement block #757 sits
    beside).
  * trajectory_invariants.py -- the invariant-harness helpers
    (all_invariant_names, the duplicate-registration guard, assert_invariant's
    failure-raise) and the no-op / violation branches of the per-year checks
    the golden run never trips (a healthy trajectory violates nothing).

Fabricated round numbers only (DP#15). No personal data. No production-code
changes -- every line here was reachable; none was dead.
"""

import math

import pytest

import decumulation
import optimize
import trajectory_invariants as ti
from simulation_config import YearResult


# =============================================================================
# decumulation.py -- shortfall_of() (issue #757's helper)
# =============================================================================

class TestShortfallOf:
    """shortfall_of() is the helper worst_drawdown_shortfall() routes every
    row through. Cover its three input shapes so its return line is exercised
    (not merely tolerated by the ratchet)."""

    def test_dict_with_summary_returns_it(self):
        """A row carrying a drawdown_shortfall summary returns that summary
        (the `return ds` half of the conditional return)."""
        summary = {'engaged': True, 'exhausted': False}
        assert decumulation.shortfall_of({'drawdown_shortfall': summary}) is summary

    def test_dict_without_key_returns_none(self):
        """A row with no drawdown_shortfall key returns None (the `else None`
        half -- distinct from a present-but-empty dict, DP#32)."""
        assert decumulation.shortfall_of({}) is None
        # A present-but-None value is an absence too (DP#32 explicit test).
        assert decumulation.shortfall_of({'drawdown_shortfall': None}) is None

    def test_non_dict_row_returns_none(self):
        """An older/synthetic non-dict row returns None (the isinstance guard)."""
        assert decumulation.shortfall_of("not a dict") is None
        assert decumulation.shortfall_of(None) is None


class TestSummarizeDrawdownShortfallEmpty:
    """The empty-results early-return in summarize_drawdown_shortfall(): a
    trajectory with no years is all-False (never engaged, never exhausted)."""

    def test_empty_results_return_all_false(self):
        summary = decumulation.summarize_drawdown_shortfall([])
        assert summary == {
            'engaged': False, 'exhausted': False,
            'first_shortfall_year': None, 'first_shortfall_gap': 0.0,
            'shortfall_years': 0, 'total_unmet': 0.0,
        }


class TestWorstDrawdownShortfallNotExhausted:
    """The `if not exhausted: return {all-False}` path: a list whose scenarios
    either never engaged or never exhausted round-trips to an all-False
    summary (DP#32: "not checked" is not "found safe")."""

    def test_no_exhausted_scenario_returns_all_false(self):
        summary = {'engaged': True, 'exhausted': False, 'first_shortfall_year': None,
                   'first_shortfall_gap': 0.0, 'shortfall_years': 0, 'total_unmet': 0.0}
        rows = [{'label': 'solvent', 'drawdown_shortfall': summary}]
        worst = decumulation.worst_drawdown_shortfall(rows)
        assert worst['exhausted'] is False
        assert worst['first_shortfall_year'] is None

    def test_rows_without_summary_return_all_false(self):
        # Older rows carrying no drawdown_shortfall at all: nothing is exhausted.
        worst = decumulation.worst_drawdown_shortfall([{'label': 'legacy'}, {}])
        assert worst['exhausted'] is False


# =============================================================================
# optimize.py -- _print_decumulation_shortfall_report (the #757 console path)
# =============================================================================

def _engaged_exhausted_summary():
    """Fabricated round numbers: a plan that ran out in year 27 (DP#15)."""
    return {'engaged': True, 'exhausted': True, 'first_shortfall_year': 27,
            'first_shortfall_gap': 205_784.0, 'shortfall_years': 19,
            'total_unmet': 3_000_000.0}


class TestPrintDecumulationShortfallReport:
    """The console deliverable #757 added. Two branches were untested: the
    'not engaged' early-return and the 'render the registered caveat' block
    (plus its per-scenario table)."""

    def test_not_engaged_prints_not_checked_notice(self, capsys):
        """No scenario engaged the drawdown -> the loud DP#32 'not checked'
        notice (NOT a '0 shortfall' falsehood)."""
        rows = [{'label': 'never retired',
                 'drawdown_shortfall': {'engaged': False, 'exhausted': False}}]
        optimize._print_decumulation_shortfall_report(rows, {})
        out = capsys.readouterr().out
        assert 'DECUMULATION NOT CHECKED' in out
        assert 'not been checked' in out

    def test_engaged_and_exhausted_renders_caveat_and_table(self, capsys):
        """An exhausted scenario renders the registered caveat block and the
        per-scenario shortfall table."""
        summary = _engaged_exhausted_summary()
        rows = [{'label': 'Bankrupt plan', 'strategy': 'b',
                 'drawdown_shortfall': summary, 'exhausted': True}]
        cfg = {'assumptions': {'decumulation_shortfall': summary}}
        optimize._print_decumulation_shortfall_report(rows, cfg)
        out = capsys.readouterr().out
        assert 'DECUMULATION SHORTFALL' in out
        # The per-scenario table names the first shortfall year and the gap.
        assert 'year 27' in out
        assert '205,784' in out


# =============================================================================
# optimize.py -- compute_net_benefit: the no-birth_year retirement-income path
# =============================================================================

class TestComputeNetBenefitRetirementBranches:
    """compute_net_benefit() has two retirement-income branches gated on
    whether the primary member has a usable birth_year. The golden / #232
    tests always supply one (1979), so the no-birth_year `else` block and the
    empty-results guard were uncovered."""

    def test_empty_results_returns_zero(self):
        assert optimize.compute_net_benefit([], {}) == 0.0

    def test_rrsp_balance_without_birth_year_uses_config_retirement_income(self):
        """When total_rrsp > 0 but no birth_year is declared, the retirement
        income is built from config (CPP + OAS + pension + LIF) rather than
        via project_retirement -- the `else` block. Returns a finite float."""
        final = YearResult(
            year=2036,
            total_assets=500_000, total_debt=200_000,
            total_rrsp=300_000, total_tfsa=100_000,
            non_reg_balance=100_000, non_reg_acb=None, resp_balance=0,
            lif_withdrawal=5_000,
        )
        # No birth_year on the primary -> the else branch fires.
        cfg = {
            'family': {'members': [
                {'role': 'primary', 'cpp_monthly_estimated': 1000,
                 'pension_income_annual': 20_000}]},
            'assumptions': {'oas_annual': 8_500},
        }
        net = optimize.compute_net_benefit([final], cfg)
        assert isinstance(net, float)
        # Sanity: with positive assets and tax savings, net benefit is finite
        # and not NaN/inf (the block must have run, not raised).
        assert math.isfinite(net)


# =============================================================================
# trajectory_invariants.py -- the invariant harness itself
# =============================================================================

class TestInvariantHarness:
    """The registry / assert helpers the golden run uses only on the happy
    path. Cover the duplicate-registration guard, the name listing, and the
    assert-raises-on-violation path (the guard that guards the guards)."""

    def test_all_invariant_names_lists_registered_checks(self):
        names = ti.all_invariant_names()
        assert 'no_negative_balances' in names
        assert 'no_nan_or_inf' in names
        # Sorted, as the docstring promises.
        assert names == sorted(names)

    def test_duplicate_registration_raises(self):
        """Re-decorating an existing name is refused (DP#9: one spelling)."""
        with pytest.raises(ValueError):
            @ti.invariant('no_negative_balances')
            def _dup(results, ctx):
                return []

    def test_assert_invariant_raises_on_a_violation(self):
        """assert_invariant() raises AssertionError (rendering the failing
        years) when a check finds a violation -- the path the healthy golden
        run never takes."""
        bogus = YearResult(year=1, total_rrsp=-5.0)
        with pytest.raises(AssertionError):
            ti.assert_invariant('no_negative_balances', [bogus], {'start_year': 2026})


class TestInvariantNoOpBranches:
    """Each opt-in / ctx-gated invariant returns [] (no-op) when the ctx key
    it needs is absent. Cover those no-op returns so the guard line is
    exercised, not merely tolerated."""

    def test_mortgage_conserves_principal_noop_without_opening(self):
        assert ti.run_invariant('mortgage_conserves_principal', [], {}) == []

    def test_undrawn_heloc_margin_noop_unless_opted_in(self):
        assert ti.run_invariant('undrawn_heloc_margin_not_booked_as_debt', [], {}) == []

    def test_non_reg_grows_noop_without_gross_return(self):
        assert ti.run_invariant('non_reg_grows_with_positive_return', [], {}) == []

    def test_rrif_minimum_noop_without_birth_year(self):
        assert ti.run_invariant('rrif_minimum_fires_from_71', [], {}) == []

    def test_resp_winds_down_noop_without_children(self):
        assert ti.run_invariant('resp_winds_down_after_children_age_out', [], {}) == []

    def test_sm_investment_has_tax_drag_noop_without_sm_balances(self):
        assert ti.run_invariant('sm_investment_has_tax_drag', [], {}) == []

    def test_estate_value_noop_without_house_value(self):
        assert ti.run_invariant(
            'estate_value_never_exceeds_pretax_balance_sheet', [], {}) == []

    def test_invested_capital_noop_without_expected_debt(self):
        assert ti.run_invariant('invested_capital_equals_new_debt', [], {}) == []

    def test_ruin_reported_noop_unless_expected(self):
        assert ti.run_invariant(
            'ruin_reported_when_shortfall_survives_waterfall', [], {}) == []


class TestInvariantViolationBranches:
    """The violation-append paths a healthy trajectory never trips. Drive
    each with a fabricated violating trajectory (round numbers, DP#15)."""

    def test_no_nan_or_inf_flags_a_nan_field(self):
        bogus = YearResult(year=1, total_assets=float('nan'))
        violations = ti.run_invariant('no_nan_or_inf', [bogus], {'start_year': 2026})
        assert violations and 'total_assets' in violations[0].message

    def test_debt_never_negative_flags_negative_debt(self):
        bogus = YearResult(year=1, mortgage_balance=-1_000.0)
        violations = ti.run_invariant('debt_never_negative', [bogus], {'start_year': 2026})
        assert violations and 'mortgage_balance' in violations[0].message

    def test_mortgage_conserves_principal_flags_a_breach(self):
        """opening balance 200k, principal paid 0, but balance jumped to 250k
        -> money appeared (conservation breach)."""
        bogus = YearResult(year=1, mortgage_balance=250_000.0, mortgage_principal=0.0)
        violations = ti.run_invariant(
            'mortgage_conserves_principal', [bogus],
            {'start_year': 2026, 'opening_mortgage_balance': 200_000.0})
        assert violations
        assert 'conservation' in violations[0].message

    def test_undrawn_heloc_margin_flags_booked_debt(self):
        """A margin declared never-drawn but appearing as heloc_balance is a
        violation (DP#18/#577: undrawn room is not debt)."""
        bogus = YearResult(year=1, heloc_balance=50_000.0)
        violations = ti.run_invariant(
            'undrawn_heloc_margin_not_booked_as_debt', [bogus],
            {'start_year': 2026, 'margin_never_drawn': True})
        assert violations
        assert 'undrawn HELOC' in violations[0].message

    def test_acb_le_fmv_flags_basis_above_market(self):
        """ACB above FMV is a violation (DP#19: cost basis never exceeds
        fair market value)."""
        bogus = YearResult(year=1, non_reg_acb=120_000.0, non_reg_balance=100_000.0)
        violations = ti.run_invariant('acb_le_fmv', [bogus], {'start_year': 2026})
        assert violations
        assert 'ACB' in violations[0].message

    def test_ruin_reported_flags_missing_ruined_flag(self):
        """A scenario declared to expect ruin, but no year reports ruined=True,
        is a violation (the discoverability property, #757-adjacent)."""
        ok = YearResult(year=1)  # not ruined
        violations = ti.run_invariant(
            'ruin_reported_when_shortfall_survives_waterfall', [ok],
            {'start_year': 2026, 'expect_ruin': True})
        assert violations
        assert 'ruined' in violations[0].message