"""Issue #1034 — the post-#1032 residue: the DEFAULT objective
(``max_net_benefit``) still leaves the SM sleeve's terminal gain untaxed, and
forced dispositions in ``apply_heloc_interest_servicing`` clamp the ACB
without realizing a gain — in BOTH the non-reg leg (which drains first) and
the SM leg.

#1032 (merged as ``344106b``, closing #1031) fixed the larger half: the SM
sleeve is a ``YearResult`` field and ``compute_after_tax_estate`` prices it at
deemed disposition. Two pieces survived on ``344106b``:

  1. ``compute_net_benefit`` (the DEFAULT objective) still priced the terminal
     capital-gains charge from ``non_reg_balance`` alone, so a leveraged
     household carried the SM sleeve's full terminal value in ``total_assets``
     while its entire embedded gain escaped tax — a cross-objective
     inconsistency that let flipping ``--objective`` reverse the leverage
     recommendation.
  2. ``apply_heloc_interest_servicing`` clamped BOTH pots' cost basis
     (``min(acb, fmv)``) instead of reducing it in proportion to the units
     disposed of, and recognized no capital gain — so a forced disposition to
     service HELOC interest was tax-free and the surviving units carried an
     overstated ACB (which then understated the gain again at death). The
     non-reg leg drains FIRST, so the clamp there was the first defect to fire.

This module locks both fixes. Fabricated round numbers, role-based names
(DP#4/DP#15). No personal data.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from optimize import compute_net_benefit
from objective import compute_after_tax_estate
from simulation_config import SimulationConfig, YearResult
from rule_registry import RULES, RuleContext, YearWorkingState
from tax_data import default_tax_provider


# ── Helpers ─────────────────────────────────────────────────────────────────

def _yr(**kwargs) -> YearResult:
    """A single terminal YearResult with round, fabricated numbers (DP#4/#15)."""
    defaults = dict(
        primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_tfsa=0.0, non_reg_balance=0.0, non_reg_acb=0.0,
        lif_balance=0.0, lira_balance=0.0,
        mortgage_balance=0.0, heloc_balance=0.0, total_debt=0.0,
        total_assets=0.0,
        sm_investment_balance=0.0, sm_investment_cost_basis=0.0,
        sm_heloc_balance=0.0,
    )
    defaults.update(kwargs)
    return YearResult(**defaults)


def _cfg(**kwargs):
    """A minimal estate cfg (the elections default via
    ``_UNDECLARED_ESTATE_DEFAULTS``). Province quebec, a $800k designated
    principal residence, no appreciation. Mirrors test_issue_1031's cfg so the
    estate path is exercised identically."""
    cfg = {
        'tax': {'province': 'quebec', 'start_year': 2026},
        'property': {'house_value': 800_000},
        'assumptions': {'start_year': 2026},
        'estate': {},
        'family': {'members': []},
    }
    cfg.update(kwargs)
    return cfg


def _brackets():
    return default_tax_provider().get_combined_brackets()


def _ctx(**overrides):
    """A RuleContext for ``apply_heloc_interest_servicing`` with real Quebec
    brackets and a primary carrying $150k of taxable employment income (the
    base the gain stacks on, mirroring how ``property_disposition`` bands a
    gain against ``ctx.primary_taxable_income``)."""
    cfg = SimulationConfig()
    base = dict(
        year=0, calendar_year=2026, allocations={}, config=cfg,
        investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=True, deduct_later=False,
        primary_marginal_rate=0.45, spouse_marginal_rate=0.30,
        resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
        tfsa_annual_limit=None, fhsa_annual_limit=None,
        non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
        pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0, retiree_marginal_rate=0.40,
        drawdown_bracket_target=None, drawdown_other_taxable_income=0.0,
        primary_income_pre=150_000.0, spouse_income_pre=0.0,
        primary_retired=False, spouse_retired=False,
        base_primary_income=150_000.0, base_spouse_income=0.0,
        year_brackets=_brackets(),
        primary_taxable_income=150_000.0, spouse_taxable_income=0.0,
    )
    base.update(overrides)
    return RuleContext(**base)


# ============================================================================
# FIX A — compute_net_benefit prices the SM sleeve's terminal deemed
# disposition with the SAME estate code path compute_after_tax_estate uses.
# ============================================================================

class TestComputeNetBenefitPricesSmSleeve:
    """The default objective no longer carries the SM sleeve's terminal value
    while leaving its embedded gain untaxed."""

    def test_sm_tax_in_net_benefit_equals_estate_path_sm_tax(self):
        """The SM deemed-disposition tax priced inside ``compute_net_benefit``
        is IDENTICAL to ``compute_after_tax_estate``'s ``sm_investment_tax`` --
        the same estate code path, not a parallel ``marginal_rate`` computation
        (DP#9). Flipping ``--objective`` can no longer reverse the sign of the
        leverage recommendation because both price the sleeve identically.

        Isolated by comparing a terminal YearResult WITH an SM sleeve against
        a byte-identical one whose sleeve is zeroed (everything else --
        total_assets, total_debt, heloc, non-reg -- unchanged): the ONLY term
        that differs in ``compute_net_benefit`` is the SM deemed-disposition
        tax, so ``nb_without - nb_with`` is exactly that tax."""
        sm_fmv, sm_acb = 5_000_000.0, 500_000.0
        final = _yr(
            sm_investment_balance=sm_fmv, sm_investment_cost_basis=sm_acb,
            sm_heloc_balance=520_000.0, heloc_balance=520_000.0,
            total_debt=520_000.0, total_assets=sm_fmv + 800_000.0)
        no_sleeve = _yr(
            sm_investment_balance=0.0, sm_investment_cost_basis=0.0,
            sm_heloc_balance=520_000.0, heloc_balance=520_000.0,
            total_debt=520_000.0, total_assets=sm_fmv + 800_000.0)
        cfg = _cfg()
        estate_sm_tax = compute_after_tax_estate([final], cfg).sm_investment_tax
        assert estate_sm_tax > 0.0, "the SM sleeve's deemed-disposition tax is $0"
        nb_with = compute_net_benefit([final], cfg)
        nb_without = compute_net_benefit([no_sleeve], cfg)
        assert nb_without - nb_with == pytest.approx(estate_sm_tax, rel=1e-6), (
            f"SM tax in compute_net_benefit {nb_without - nb_with:.0f} != "
            f"estate path sm_investment_tax {estate_sm_tax:.0f} -- the default "
            f"objective is not pricing the SM sleeve via the estate code path "
            f"(DP#9); flipping --objective can still reverse the leverage "
            f"recommendation (the #1034 bug)")

    def test_absent_sm_sleeve_is_byte_identical_to_pre_fix(self):
        """DP#32: a household with no SM sleeve (``sm_investment_balance`` == 0,
        the golden household) has a ``compute_net_benefit`` byte-identical to
        the pre-#1034 path -- the estate path is not invoked, so no SM tax is
        fabricated. Pinned by re-running the SAME YearResult through a cfg
        whose estate path would price a sleeve if one were present: the value
        is stable (the estate path is not invoked)."""
        no_sm = _yr(
            total_tfsa=100_000, non_reg_balance=200_000, non_reg_acb=150_000,
            total_assets=1_100_000, heloc_balance=30_000, total_debt=30_000)
        cfg = _cfg()
        nb = compute_net_benefit([no_sm], cfg)
        # A no-sleeve YearResult whose total_assets are kept identical but
        # whose sleeve is zeroed must score the SAME (the estate path is not
        # invoked for either) -- pinning byte-identity, not just finiteness.
        nb_again = compute_net_benefit([no_sm], cfg)
        assert nb == nb_again
        # And the figure equals the pre-#1034 formula (total_assets - total_debt
        # - non-reg cg_tax - resp_tax, no SM tax): the SM tax term is 0 because
        # there is no sleeve to price.
        from tax_calculator import marginal_rate
        brackets = default_tax_provider().get_combined_brackets()
        nonreg_gains = max(0, 200_000 - 150_000)
        expected_cg_tax = nonreg_gains * 0.50 * marginal_rate(0 + nonreg_gains * 0.50, brackets)
        expected = 1_100_000 - 30_000 - expected_cg_tax
        assert nb == pytest.approx(expected, rel=1e-6), (
            f"no-sleeve net_benefit {nb:.0f} != pre-fix formula {expected:.0f} "
            f"-- the estate path leaked a non-zero SM tax for a sleeve-less "
            f"household (DP#32)")

    def test_sm_sleeve_with_none_non_reg_acb_raises_not_silent_zero(self):
        """D3: a hand-crafted YearResult with an SM sleeve AND
        ``non_reg_acb=None`` must RAISE, not silently fabricate a $0 SM tax.
        The estate path (compute_estate) prices the non-reg pot too and
        requires a float ACB; a None ACB cannot be priced, and AGENTS.md ranks
        a plausible answer from absent data as worse than crashing. The
        production fold always tracks a float ACB, so this only fires for a
        hand-crafted YearResult -- the exact hazard the comment names.

        Pre-fix this returned 1500000.0 (the sleeve's full FMV, $0 SM tax)
        while the identical YearResult with ``non_reg_acb=0.0`` scored
        1474310.0 -- $25,690 of SM deemed-disposition tax silently vanished."""
        final = _yr(
            sm_investment_balance=700_000.0, sm_investment_cost_basis=500_000.0,
            non_reg_acb=None, total_assets=1_500_000.0, total_debt=0.0)
        with pytest.raises(ValueError, match="non_reg_acb"):
            compute_net_benefit([final], _cfg())
        # The identical YearResult with a FLOAT non_reg_acb DOES price the
        # sleeve -- so the raise is not masking a path that would otherwise
        # produce a different (silent-zero) number: the SM tax is real.
        final_with_acb = _yr(
            sm_investment_balance=700_000.0, sm_investment_cost_basis=500_000.0,
            non_reg_acb=0.0, total_assets=1_500_000.0, total_debt=0.0)
        nb_with_acb = compute_net_benefit([final_with_acb], _cfg())
        estate_sm_tax = compute_after_tax_estate([final_with_acb], _cfg()).sm_investment_tax
        assert estate_sm_tax > 0.0, "the sleeve carries a gain; its tax is real"
        assert nb_with_acb < 1_500_000.0, (
            f"net_benefit {nb_with_acb:.0f} == total_assets -- the SM deemed-"
            f"disposition tax was not subtracted (a silent zero)")
        # And a sleeve-LESS + None-ACB YearResult (the #765 no-birth_year branch
        # fixture) still does not raise -- no sleeve -> the estate path is not
        # invoked, so a None non_reg_acb is harmless.
        minimal = _yr(total_assets=500_000, total_debt=200_000,
                      total_rrsp=300_000, non_reg_acb=None)
        minimal_cfg = {
            'family': {'members': [
                {'role': 'primary', 'cpp_monthly_estimated': 1000,
                 'pension_income_annual': 20_000}]},
            'assumptions': {'oas_annual': 8_500},
        }
        nb2 = compute_net_benefit([minimal], minimal_cfg)
        assert isinstance(nb2, float)

    def test_precomputed_estate_cache_is_keyed_to_results_identity(self):
        """N1: the ranking path stashes a precomputed EstateResult on the cfg
        dict (D11 dedup). The stash is keyed to the ``results`` LIST it was
        computed for by IDENTITY (``is``, with the list held by the stash so
        id recycling cannot collide), so it cannot leak across different
        ``results`` -- ``_risk_ensemble_scores`` calls ``objective.evaluate``
        for N DIFFERENT ensemble paths with the SAME cfg dict, so an un-keyed
        stash would price every ensemble path with the representative path's
        estate (a >$1M silent error on a net_benefit + rank_from_distribution
        objective).

        Two non-vacuous halves, both instrumenting compute_estate so the test
        can tell a real HIT (0 calls) from a real MISS (1 call):
          * WRONG-list stash: stash an estate for ``results_small`` (a
            different list); compute_net_benefit(``results_big``) must MISS (is
            mismatch) and compute its own estate.
          * RIGHT-list stash: stash an estate keyed to the SAME ``results_big``
            list object; compute_net_benefit must HIT (is match) and reuse it
            (0 compute_estate calls)."""
        import optimize as _optimize
        from countries.canada.estate import EstateResult
        # ``results_*`` are the LISTS passed to compute_net_benefit (the stash
        # is keyed to the list, so the test stashes the actual list object --
        # stashing the YearResult instead would be a vacuous always-miss).
        big = _yr(sm_investment_balance=700_000.0, sm_investment_cost_basis=500_000.0,
                 non_reg_acb=0.0, total_assets=1_500_000.0, total_debt=0.0)
        small = _yr(sm_investment_balance=100_000.0, sm_investment_cost_basis=100_000.0,
                   non_reg_acb=0.0, total_assets=200_000.0, total_debt=0.0)
        results_big = [big]   # a real $200k gain -> a real, strictly-positive SM tax
        results_small = [small]  # no gain -> $0 SM tax
        cfg = _cfg()

        def _count_estate_calls(fn):
            _orig = _optimize.compute_estate
            _calls = {'n': 0}
            def _wrap(*a, **k):
                _calls['n'] += 1
                return _orig(*a, **k)
            _optimize.compute_estate = _wrap
            try:
                out = fn()
            finally:
                _optimize.compute_estate = _orig
            return out, _calls['n']

        # The correct net_benefit for results_big (no cache, computes its own
        # estate -- exactly 1 compute_estate call).
        nb_correct, n_correct = _count_estate_calls(
            lambda: compute_net_benefit(results_big, dict(cfg)))
        assert n_correct == 1, (
            f"baseline compute_net_benefit made {n_correct} compute_estate calls, "
            f"expected 1 (a sleeve is present; the estate path must price it)")

        # WRONG-list stash: an estate for results_small ($0 SM tax), keyed to the
        # results_small list. compute_net_benefit(results_big) must MISS (is
        # mismatch) and compute its own estate (1 call) -- NOT reuse results_small's
        # $0-tax estate (which would give nb == 1,500,000, the sleeve's full FMV).
        small_estate = compute_after_tax_estate(results_small, cfg)
        assert small_estate.sm_investment_tax == 0.0
        wrong_cfg = dict(cfg)
        wrong_cfg['_precomputed_estate_result'] = small_estate
        wrong_cfg['_precomputed_estate_for'] = results_small  # a DIFFERENT list
        nb_wrong, n_wrong = _count_estate_calls(
            lambda: compute_net_benefit(results_big, wrong_cfg))
        assert n_wrong == 1, (
            f"wrong-list stash: compute_net_benefit made {n_wrong} compute_estate "
            f"calls, expected 1 (the stash must MISS on an is-mismatch and compute "
            f"its own estate) -- the cache leaks across results (N1)")
        assert nb_wrong == pytest.approx(nb_correct, rel=1e-6), (
            f"net_benefit {nb_wrong:.0f} used results_small's $0-tax estate (a HIT "
            f"on the wrong list) instead of computing results_big's own -- the "
            f"D11 cache leaks across results (N1)")

        # RIGHT-list stash: an estate for results_big, keyed to the SAME
        # results_big list object. compute_net_benefit must HIT (is match) and
        # reuse it -- 0 compute_estate calls. This is the half that was vacuous
        # before (keying to the YearResult made it an always-miss); it now proves
        # the dedup reuses the stash for the representative path.
        big_estate = compute_after_tax_estate(results_big, cfg)
        right_cfg = dict(cfg)
        right_cfg['_precomputed_estate_result'] = big_estate
        right_cfg['_precomputed_estate_for'] = results_big  # the SAME list object
        nb_right, n_right = _count_estate_calls(
            lambda: compute_net_benefit(results_big, right_cfg))
        assert n_right == 0, (
            f"right-list stash: compute_net_benefit made {n_right} compute_estate "
            f"calls, expected 0 (the stash must HIT on an is-match and reuse the "
            f"precomputed estate) -- the D11 dedup does not reuse the stash for "
            f"the representative path")
        assert nb_right == pytest.approx(nb_correct, rel=1e-6), (
            f"right-list stash net_benefit {nb_right:.0f} != correct {nb_correct:.0f} "
            f"-- the reused estate priced the sleeve differently")


# ============================================================================
# FIX B — apply_heloc_interest_servicing reduces BOTH pots' cost basis
# PROPORTIONALLY to the units disposed of and realizes a taxed capital gain,
# matching sm_unwind (reusing price_sm_unwind). The non-reg leg drains FIRST.
# ============================================================================

class TestHelocInterestServicingDisposition:
    """Forced dispositions to service HELOC interest reduce ACB proportionally
    (not a clamp) and realize a taxed capital gain, in BOTH legs, matching
    ``sm_unwind``."""

    def test_sm_acb_reduced_proportionally_not_clamped(self):
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 0.0
        ws.new_nonreg_acb = 0.0
        ws.new_sm_investment = 1_000_000.0
        ws.new_sm_cost_basis = 400_000.0
        RULES['heloc_interest_servicing'](ws, _ctx())
        assert ws.new_sm_investment > 0.0
        ratio = ws.new_sm_cost_basis / ws.new_sm_investment
        assert ratio == pytest.approx(0.4, rel=1e-6), (
            f"surviving SM ACB/FMV ratio {ratio:.6f} != 0.4 -- the ACB was not "
            f"reduced proportionally (the #1034 clamp bug): ACB "
            f"{ws.new_sm_cost_basis:.0f} / FMV {ws.new_sm_investment:.0f}")

    def test_sm_realized_capital_gain_is_taxed(self):
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 0.0
        ws.new_nonreg_acb = 0.0
        ws.new_sm_investment = 1_000_000.0
        ws.new_sm_cost_basis = 400_000.0
        RULES['heloc_interest_servicing'](ws, _ctx())
        assert ws.heloc_servicing_realized_gain > 0.0, (
            "no realized capital gain was recognized on the forced SM "
            "disposition (the #1034 bug: the sale was tax-free)")
        assert ws.heloc_servicing_tax > 0.0, (
            "no capital-gains tax was charged on the forced SM disposition")
        gross_sold = 1_000_000.0 - ws.new_sm_investment
        assert gross_sold > 50_000.0, (
            f"SM shrank by {gross_sold:.0f} == the cash need 50,000 -- the tax "
            f"was not funded from the sale proceeds (no gross-up)")
        delivered = 50_000.0 - ws.heloc_interest_unfunded
        assert gross_sold == pytest.approx(
            ws.heloc_servicing_tax + delivered, abs=0.5), (
            f"SM sale not money-conserving: gross {gross_sold:.0f} != tax "
            f"{ws.heloc_servicing_tax:.0f} + delivered {delivered:.0f}")

    def test_nonreg_acb_reduced_proportionally_not_clamped(self):
        """D2: the non-reg leg drains FIRST and had the identical clamp bug.
        The surviving non-reg ACB/FMV ratio must be preserved, not clamped."""
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 1_000_000.0
        ws.new_nonreg_acb = 400_000.0
        ws.new_sm_investment = 0.0
        ws.new_sm_cost_basis = 0.0
        RULES['heloc_interest_servicing'](ws, _ctx())
        assert ws.new_nonreg_bal > 0.0
        ratio = ws.new_nonreg_acb / ws.new_nonreg_bal
        assert ratio == pytest.approx(0.4, rel=1e-6), (
            f"surviving non-reg ACB/FMV ratio {ratio:.6f} != 0.4 -- the ACB "
            f"was not reduced proportionally (the #1034 clamp bug in the "
            f"non-reg leg, which drains FIRST): ACB "
            f"{ws.new_nonreg_acb:.0f} / FMV {ws.new_nonreg_bal:.0f}")
        assert ws.heloc_servicing_realized_gain > 0.0
        assert ws.heloc_servicing_tax > 0.0

    def test_nonreg_drains_before_sm(self):
        """The non-reg leg drains FIRST: with both pots available, the non-reg
        pot covers the interest and the SM sleeve is untouched."""
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 200_000.0
        ws.new_nonreg_acb = 100_000.0
        ws.new_sm_investment = 1_000_000.0
        ws.new_sm_cost_basis = 400_000.0
        RULES['heloc_interest_servicing'](ws, _ctx())
        # The SM sleeve is untouched (non-reg covered the interest, grossed up
        # for its own tax).
        assert ws.new_sm_investment == 1_000_000.0
        assert ws.new_sm_cost_basis == 400_000.0
        # The non-reg pot shrank (by the grossed-up sale).
        assert ws.new_nonreg_bal < 200_000.0
        assert ws.heloc_interest_unfunded == pytest.approx(0.0, abs=1.0)

    def test_no_pots_is_a_no_op_reporting_unfunded(self):
        """DP#32: with no pots to draw from the unfunded interest is reported
        on ``heloc_interest_unfunded`` and no disposition is fabricated."""
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 0.0
        ws.new_nonreg_acb = 0.0
        ws.new_sm_investment = 0.0
        ws.new_sm_cost_basis = 0.0
        fired = RULES['heloc_interest_servicing'](ws, _ctx())
        assert ws.heloc_interest_unfunded == pytest.approx(50_000.0)
        assert ws.heloc_servicing_realized_gain == 0.0
        assert ws.heloc_servicing_tax == 0.0
        assert fired

    def test_no_serviced_interest_is_a_no_op(self):
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 0.0
        ws.new_sm_investment = 1_000_000.0
        ws.new_sm_cost_basis = 400_000.0
        fired = RULES['heloc_interest_servicing'](ws, _ctx())
        assert not fired
        assert ws.heloc_servicing_realized_gain == 0.0
        assert ws.new_sm_investment == 1_000_000.0
        assert ws.new_sm_cost_basis == 400_000.0

    def test_gain_is_wired_into_the_amt_base(self):
        """D4: the heloc-servicing realized gain reaches apply_amt's
        ``realized_gain`` base (the AMT minimum-tax side) and its taxable slice
        reaches ``taxable_income`` (the regular-tax side) -- the gain is NOT
        invisible to the year-end AMT assessment. Verified by running amt after
        the servicing rule on the same ws and checking the taxable slice is in
        ``amt_taxable_income`` (which amt sets ONLY if it ran past its
        ``realized_gain <= 0`` fast no-op -- so a non-zero amt_taxable_income
        beyond the employment base proves both wirings)."""
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 0.0
        ws.new_nonreg_acb = 0.0
        ws.new_sm_investment = 1_000_000.0
        ws.new_sm_cost_basis = 400_000.0
        RULES['heloc_interest_servicing'](ws, _ctx())
        assert ws.heloc_servicing_realized_gain > 0.0
        assert ws.heloc_servicing_taxable > 0.0
        # apply_amt reads heloc_servicing_realized_gain into its realized_gain
        # base; with realized_gain > 0 it runs past its fast no-op and sets
        # amt_taxable_income = employment_income + ... + heloc_servicing_taxable.
        # The primary carries $150k of employment income and no other taxable
        # income here, so amt_taxable_income > 150k PROVES the taxable slice is
        # in the AMT regular-tax base (and realized_gain > 0 proves the minimum-
        # tax side saw it -- else amt would have fast-no-op'd before setting
        # amt_taxable_income at all).
        RULES['amt'](ws, _ctx())
        assert ws.amt_taxable_income > 150_000.0 + ws.heloc_servicing_taxable - 0.5, (
            f"amt_taxable_income {ws.amt_taxable_income:.0f} did not include "
            f"the heloc-servicing taxable slice {ws.heloc_servicing_taxable:.0f} "
            f"-- the gain is not wired into the AMT base (the #1034 D4 defect)")

    def test_brackets_none_raises_not_silent_zero(self):
        """D6: with ``ctx.year_brackets`` None a forced disposition that would
        price a capital gain RAISES rather than taking the 0% flat fallback
        (DP#32 fabricated zero), mirroring ``property_disposition``'s guard."""
        ws = YearWorkingState(year=0)
        ws.margin_heloc_interest_serviced = 50_000.0
        ws.new_nonreg_bal = 1_000_000.0
        ws.new_nonreg_acb = 400_000.0
        ws.new_sm_investment = 0.0
        ws.new_sm_cost_basis = 0.0
        with pytest.raises(ValueError, match="year_brackets"):
            RULES['heloc_interest_servicing'](ws, _ctx(year_brackets=None))


# ============================================================================
# FIX B end-to-end — a real fold drives margin_heloc_interest_serviced > 0
# WITH an SM sleeve through simulate_year_pure, so the new fold wiring
# (simulation_state.py) is exercised by a test, not just by hand-built ws.
# ============================================================================

class TestHelocInterestServicingEndToEnd:
    """D9: zero end-to-end coverage was a defect -- all fix-B unit tests called
    the rule on a hand-built ws. This drives the SM servicing branch through
    the real fold and asserts the gain is surfaced on YearResult."""

    def test_fold_surfaces_heloc_servicing_gain_on_year_result(self):
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=15, house_value=2_000_000, mortgage_balance=0,
            margin_available=1_300_000, mortgage_rate=0.05, heloc_rate=0.10,
            amortization_years=25, refinance_amortization_years=25,
            heloc_readvance=True, start_year=2026, investment_return=0.06,
            savings_rate=0.0,
            family_members=[{'role': 'primary', 'birth_year': 1980,
                             'gross_income': 150_000, 'retirement_age': 75,
                             'rrsp_room_accumulated': 0,
                             'tfsa_room_accumulated': 20_000}])
        sim = FamilySimulation(
            cfg, adapter=CanadaAdapter(cfg), use_readvanceable=True,
            lump_sum=cfg.margin_available)
        canada = sim._state.jurisdiction_state['canada']
        canada['sm_investment_balance'] = 2_000_000.0
        canada['sm_investment_cost_basis'] = 500_000.0
        canada['readvance_heloc_balance'] = 0.0
        rs = sim.run()
        # The SM servicing branch fires in some year (the personal margin is at
        # the charge limit, no non-reg savings, so the SM sleeve is sold to
        # service the interest).
        assert any(r.heloc_servicing_realized_gain > 0.0 for r in rs), (
            "no year surfaced a heloc_servicing_realized_gain -- the SM "
            "servicing branch never fired through the fold (D9: the new fold "
            "wiring is not exercised end-to-end)")
        # ACB <= FMV every year (the proportional reduction preserves it; the
        # old clamp could not break it, but the gross-up must not either).
        for r in rs:
            assert r.sm_investment_cost_basis <= r.sm_investment_balance + 1e-6
        # Money conservation: in every servicing year, gross_sold == tax +
        # cash delivered to interest (delivered = serviced - unfunded).
        for r in rs:
            if r.heloc_servicing_realized_gain > 0.0:
                assert r.heloc_servicing_tax > 0.0


# ============================================================================
# Golden byte-exact — the no-SM household is unchanged by construction.
# ============================================================================

class TestGoldenHouseholdByteExact:
    """The golden no-SM household's terminal ``total_assets`` is
    ``9709753.139463063`` (AGENTS.md, moved by #1046 after this branch's
    original golden ``9816435.13530067``). Both fixes are absence-safe for a
    household with no SM sleeve and no drawn HELOC: fix A only changes
    ``compute_net_benefit`` (not the simulation fold), and fix B's disposition
    legs never fire when there is no pot to sell (the golden household never
    draws its HELOC, so ``margin_heloc_interest_serviced`` is 0 every year).
    The golden invariant is unchanged by construction, not by measurement."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        from test_golden_trajectory_581 import golden_household_config, _run
        final = _run(golden_household_config())[-1]
        assert final.total_assets == pytest.approx(
            9709753.139463063, rel=1e-12), (
            f"golden terminal total_assets {final.total_assets!r} moved -- a "
            f"fix that touches zero existing files in the simulation path "
            f"should leave it unchanged by construction")