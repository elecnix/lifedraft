"""Issue #1081 -- ``min_after_tax_estate`` rewarded TAKING ON DEBT in the
solvent regime: "minimize the estate" is not "die with zero".

``net_estate = assets - debts - terminal_tax``, and the pre-#1081 score was
``-net_estate`` (solvent regime). Debt therefore reduced the score's burden
dollar-for-dollar: a strategy that BORROWED and did not repay bought exactly
one point of score per borrowed dollar while total assets stayed flat. That
is not an edge case -- it is the objective's central gradient, and it is live
in the SOLVENT regime, so #1065's insolvency pricing (which fires only on
``net_estate < 0``) never executes for it.

Two independent reproductions from the issue:

1. **Via #1042's borrow-to-invest, ``liquidate_to_target`` absent** -- every
   borrowed dollar left outstanding buys one dollar of score while terminal
   assets are IDENTICAL across draw rungs, and "borrow the maximum" is
   printed as the top recommendation. #1042 is not yet merged, so this file
   reproduces it at the REAL objective/optimizer path (the registered
   ``ObjectiveFunction`` resolved by ``optimize.resolve_objective``, ranked
   by ``evaluate`` exactly as the optimizers rank) over the terminal balance
   sheets the feature's report prints -- plus an ENGINE-level analogue (a
   consumer-loan ladder, #763, both on main) where the borrowing rides to
   death inside a real fold.

2. **Via a home sale, using only features already on main** (#763 consumer
   loans + #956/#964 principal sale): SELL-and-die-owing-$50k outranked
   KEEP-with-+$600k. Reproduced here END-TO-END through the engine.

The fix (objective.py ``_neg_after_tax_estate`` + ``EstateResult.
drawable_after_tax``): the score is

    -(drawable_after_tax) - debts - insolvency

where ``drawable_after_tax`` is every estate pot EXCEPT the designated
principal residence after its own deemed-disposition tax. Debt enters ONLY
as a penalty -- never netted against assets -- so borrowing buys zero score;
the residence sits outside the spend-down surface (it is consumed by living
in it), which is what makes KEEP-home outrank SELL-and-die-owing; and the
#1065 insolvency term is retained so dying owing $X stays strictly worse than
dying solvent with $X unspent.

Acceptance pinned here:
  - reproduction (1): ``no_draw`` ranks at or above every borrowing rung;
  - reproduction (2): KEEP ranks ABOVE SELL (end-to-end engine runs);
  - the general property: for otherwise-identical trajectories, more
    terminal debt NEVER scores higher (grid-checked across the solvent AND
    insolvent regimes);
  - the golden invariant stays byte-exact (the diff touches no fold file).

Fabricated round numbers, role-based names (DP#4/DP#15).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import pytest

from test_golden_trajectory_581 import golden_household_config, _run
from test_issue_763_consumer_loans import _closed_end_liability, _doc_with_liabilities
from test_issue_956_bite_e_principal_sale import _add_principal_sale
from test_issue_1009_die_with_zero import _yr
from test_issue_1065_insolvent_estate_scoring import (
    _insolvent_die_with_zero_contract, _run_contract,
)

import copy

import input_contract as ic
from countries.canada.estate import EstateResult
from objective import (
    MAX_AFTER_TAX_ESTATE,
    MIN_AFTER_TAX_ESTATE,
    compute_after_tax_estate,
)
import optimize


# ── Helpers ─────────────────────────────────────────────────────────────────

_HOUSE_VALUE = 200_000.0


def _rung_cfg():
    """A config giving the synthetic rung years a residence (the borrow-to-
    invest household of reproduction (1) keeps its home across all rungs --
    only the outstanding HELOC differs)."""
    return {'property': {'house_value': _HOUSE_VALUE}}


def _btv_rung(draw):
    """One borrow-to-invest rung's TERMINAL balance sheet, in the shape
    #1042's report prints: the drawn dollars are entirely gone (terminal
    ``non_reg_balance == 0``), total assets are IDENTICAL across rungs, and
    the outstanding HELOC is higher by exactly the draw. Fabricated round
    numbers (DP#4/#15)."""
    return [_yr(
        total_tfsa=21_041.85,
        non_reg_balance=0.0,
        heloc_balance=float(draw),
        total_debt=float(draw),
        total_assets=221_041.85,
    )]


_BTV_RUNGS = [('no_draw', 0), ('btv_50k', 50_000),
              ('btv_100k', 100_000), ('btv_150k', 150_000)]


def _rank(names_and_trajectories, cfg):
    """Rank (name, trajectory) pairs the way the optimizers do: resolve the
    objective BY NAME through the registry seam (DP#22 -- the same path
    ``decisions.objective`` / ``--objective`` takes) and sort by
    ``ObjectiveFunction.evaluate`` (higher is better)."""
    obj = optimize.resolve_objective('min_after_tax_estate', cfg)
    return sorted(names_and_trajectories,
                  key=lambda nt: obj.evaluate(nt[1], cfg), reverse=True)


# ============================================================================
# Reproduction (1) — borrow-to-invest rungs: borrowing buys ZERO score.
# ============================================================================

class TestBorrowToInvestRungs:
    """The issue's table: four draw rungs with IDENTICAL terminal assets and
    HELOCs higher by exactly the draw. Pre-#1081 each borrowed dollar bought
    exactly one point of score and "borrow the maximum" ranked FIRST."""

    def test_no_draw_ranks_above_every_borrowing_rung(self):
        ranked = _rank([(n, _btv_rung(d)) for n, d in _BTV_RUNGS], _rung_cfg())
        assert ranked[0][0] == 'no_draw', (
            f"top row {ranked[0][0]!r} -- 'borrow the maximum' is still being "
            f"recommended (the #1081 inversion)")

    def test_ranking_is_strictly_monotone_in_the_draw(self):
        ranked = _rank([(n, _btv_rung(d)) for n, d in _BTV_RUNGS], _rung_cfg())
        assert [n for n, _ in ranked] == ['no_draw', 'btv_50k',
                                          'btv_100k', 'btv_150k']

    def test_each_borrowed_dollar_buys_zero_score_not_one(self):
        """The central gradient, priced: adjacent rungs differ by exactly
        -$50,000 of score per $50,000 borrowed (debt is a pure penalty).
        Pre-#1081 the SAME balance sheets scored the BIGGER borrower +$50,000
        HIGHER -- asserted below against the production ``net_estate`` field
        so this test is provably load-bearing (it fails on the old scoring,
        without reimplementing it: the old score is just the negated
        production estate value)."""
        scores = {n: MIN_AFTER_TAX_ESTATE.evaluate(_btv_rung(d), _rung_cfg())
                  for n, d in _BTV_RUNGS}
        assert scores['btv_100k'] - scores['btv_150k'] == pytest.approx(50_000.0)
        assert scores['no_draw'] - scores['btv_50k'] == pytest.approx(50_000.0)
        # Load-bearing proof: under the PRE-#1081 score (-net_estate, read off
        # the production estate), the maximal borrower ranked FIRST.
        old_scores = {n: -compute_after_tax_estate(_btv_rung(d),
                                                   _rung_cfg()).net_estate
                      for n, d in _BTV_RUNGS}
        old_top = max(old_scores, key=old_scores.get)
        assert old_top == 'btv_150k'

    def test_ranking_is_the_same_with_liquidate_to_target_declared(self):
        """Acceptance: ``no_draw`` at or above every borrowing rung with
        ``liquidate_to_target`` BOTH present and absent. The score is a
        function of the terminal balance sheet; the flag changes the
        TRAJECTORY that produces it (whether residual drawables are swept),
        not how a balance sheet is scored -- so the corrected ranking holds
        under both declarations. The flag's presence here also documents that
        the objective reads nothing from it (no hidden coupling)."""
        cfg_on = dict(_rung_cfg(), retirement={'liquidate_to_target': True})
        ranked_on = _rank([(n, _btv_rung(d)) for n, d in _BTV_RUNGS], cfg_on)
        assert ranked_on[0][0] == 'no_draw'
        assert [n for n, _ in ranked_on] == ['no_draw', 'btv_50k',
                                             'btv_100k', 'btv_150k']


class TestConsumerLoanLadderEndToEnd:
    """An ENGINE-level analogue of reproduction (1) from features already on
    main: the same household carrying a $0 / $50k / $100k / $150k unsecured
    personal loan (#763, interest-only, term outlives the horizon) so the
    borrowing rides to death inside a real fold. The bigger borrower services
    more debt and dies owing more, so its score must be strictly lower every
    rung -- borrowing to spend must never rank first."""

    def test_bigger_loan_never_ranks_higher(self):
        """Each rung is a REAL engine fold; the rungs are deliberately NOT
        otherwise-identical (the bigger borrower services more debt for 50
        years and so consumes more -- which is die-with-zero PROGRESS, not
        score). The property under test is the DEBT TERM itself: for each
        rung's actual terminal balance sheet, the otherwise-identical
        debt-free counterfactual must score strictly higher, by exactly the
        outstanding debt -- i.e. on real engine output, carrying the debt to
        death never helped, and erasing it would price dollar-for-dollar.
        (The otherwise-identical-rungs ranking property is pinned at the
        objective path above, matching the issue's identical-assets table.)"""
        ladder = []
        for bal in (0, 50_000, 100_000, 150_000):
            if bal:
                personal = _closed_end_liability(
                    "personal_loan", balance=bal, rate=0.05,
                    payment_monthly=bal * 0.004 + 1, years=60)
                doc = _doc_with_liabilities(personal, keep_mortgage=True)
            else:
                doc = _doc_with_liabilities(keep_mortgage=True)
            rs, cfg = _run_contract(doc)
            ladder.append((bal, rs, cfg))
        obj = optimize.resolve_objective('min_after_tax_estate', {})
        import dataclasses
        for bal, rs, cfg in ladder:
            final = rs[-1]
            estate = compute_after_tax_estate(rs, cfg)
            if estate.debts == 0.0:
                continue  # the no-loan rung has no debt term to isolate
            # Counterfactual: the SAME terminal year, debt extinguished.
            cf = [dataclasses.replace(final, heloc_balance=0.0,
                                      total_debt=0.0)]
            s_real = obj.evaluate([final], cfg)
            s_cf = obj.evaluate(cf, cfg)
            assert s_cf > s_real, (
                f"${bal}-loan rung: extinguishing ${estate.debts:.0f} of "
                f"terminal debt did not improve the score -- debt is buying "
                f"score on a real trajectory")
            # Exactly dollar-for-dollar: drawable_after_tax is debt-invariant
            # (net_estate rises by the same dollars debts adds), so the whole
            # margin is the pure debt penalty.
            assert s_cf - s_real == pytest.approx(estate.debts)


# ============================================================================
# Reproduction (2) — home sale: KEEP-with-+$600k outranks SELL-owing-$50k.
# ============================================================================

class TestHomeSaleKeepVsSell:
    """The issue's second reproduction, END-TO-END on the real engine: the
    #1065 D1 contract (unsecured $300k personal loan + principal sale +
    liquidate_to_target) is the SELL branch; the SAME contract WITHOUT the
    sale is the KEEP branch. Pre-#1081 the SELL branch (+$50k fabricated
    bonus from ``-net_estate`` on a negative estate) outranked KEEP
    (-$600k). Post-#1081 KEEP ranks ABOVE SELL."""

    def _sell_doc(self):
        return _insolvent_die_with_zero_contract()

    def _keep_doc(self):
        doc = copy.deepcopy(self._sell_doc())
        props = doc.get('properties')
        if props:
            for p in props:
                if isinstance(p, dict) and 'sale' in p:
                    del p['sale']
        return doc

    def test_keep_outranks_sell(self):
        sell_rs, sell_cfg = _run_contract(self._sell_doc())
        keep_rs, keep_cfg = _run_contract(self._keep_doc())
        sell_estate = compute_after_tax_estate(sell_rs, sell_cfg)
        keep_estate = compute_after_tax_estate(keep_rs, keep_cfg)
        # The issue's exact shape: SELL dies owing (negative net estate),
        # KEEP dies with a large positive one (the residence remains).
        assert sell_estate.net_estate == pytest.approx(-50_000.0)
        assert keep_estate.net_estate == pytest.approx(600_000.0)
        obj = optimize.resolve_objective('min_after_tax_estate', {})
        sell_score = obj.evaluate(sell_rs, sell_cfg)
        keep_score = obj.evaluate(keep_rs, keep_cfg)
        assert keep_score > sell_score, (
            f"KEEP-home ({keep_score:.2f}) does not outrank SELL-and-die-"
            f"owing ({sell_score:.2f}) -- converting a residence into debt "
            f"is still being rewarded")
        # The margin is exactly the insolvency depth ($50k): the residence is
        # outside the spend-down surface for both branches (KEEP holds it,
        # SELL already converted and spent it), both owe the same $300k loan,
        # and only SELL dies insolvent.
        assert keep_score - sell_score == pytest.approx(
            sell_estate.insolvency)

    def test_sell_branch_still_scores_below_a_clean_zero_death(self):
        """The SELL branch must ALSO stay below a clean $0 death (the #1065
        acceptance, re-pinned under the #1081 scoring)."""
        sell_rs, sell_cfg = _run_contract(self._sell_doc())
        clean_zero = MIN_AFTER_TAX_ESTATE.evaluate([_yr()], {})
        assert clean_zero == 0.0
        assert MIN_AFTER_TAX_ESTATE.evaluate(sell_rs, sell_cfg) < clean_zero


# ============================================================================
# The general property — more terminal debt NEVER scores higher.
# ============================================================================

class TestDebtNeverBuysScore:
    """For any two otherwise-identical trajectories, the one with more
    terminal debt never scores higher -- grid-checked across asset levels and
    debt levels that cross the solvent/insolvent boundary (the #1065 regime
    AND the #1081 solvent regime must obey the SAME monotonicity)."""

    @pytest.mark.parametrize("assets", [0.0, 100_000.0, 500_000.0])
    @pytest.mark.parametrize("debt", [1.0, 50_000.0, 100_000.0,
                                      200_000.0, 500_000.0])
    def test_score_is_strictly_decreasing_in_terminal_debt(self, assets, debt):
        deeper = [_yr(total_tfsa=assets, total_assets=assets,
                      heloc_balance=debt, total_debt=debt)]
        shallower = [_yr(total_tfsa=assets, total_assets=assets)]
        s_deeper = MIN_AFTER_TAX_ESTATE.evaluate(deeper, {})
        s_shallower = MIN_AFTER_TAX_ESTATE.evaluate(shallower, {})
        assert s_deeper < s_shallower, (
            f"assets={assets}: carrying ${debt} of terminal debt scored "
            f"{s_deeper:.2f}, not strictly below the debt-free "
            f"{s_shallower:.2f} -- debt is still buying score")

    def test_debt_penalty_is_dollar_for_dollar_even_when_insolvent(self):
        """The gradient is preserved on BOTH sides of zero (no sentinel
        collapse -- the #850 trap): each extra dollar of terminal debt costs
        exactly one point of score (two once the estate is insolvent and the
        #1065 term stacks)."""
        d100 = MIN_AFTER_TAX_ESTATE.evaluate(
            [_yr(heloc_balance=100_000.0, total_debt=100_000.0)], {})
        d200 = MIN_AFTER_TAX_ESTATE.evaluate(
            [_yr(heloc_balance=200_000.0, total_debt=200_000.0)], {})
        assert d200 - d100 == pytest.approx(-200_000.0)


# ============================================================================
# The mirror claim is now SCOPED — and the divergence IS the fix.
# ============================================================================

class TestMirrorIsScopedToTheDebtFreeResidenceFreeSlice:
    """Pre-#1081 the objective was documented as the exact negation of
    ``max_after_tax_estate``. That mirror is precisely what made debt buy
    score. It survives ONLY on the debt-free, residence-free slice of balance
    sheets; wherever debt or residence equity exists, the objectives
    deliberately diverge."""

    def test_mirror_holds_on_the_debt_free_residence_free_slice(self):
        small = [_yr(total_tfsa=10_000, total_assets=10_000)]
        large = [_yr(total_tfsa=800_000, total_assets=800_000)]
        for traj in (small, large):
            assert (MIN_AFTER_TAX_ESTATE.evaluate(traj, {})
                    == pytest.approx(-MAX_AFTER_TAX_ESTATE.evaluate(traj, {})))

    def test_mirror_diverges_when_debt_is_present(self):
        indebted = [_yr(total_tfsa=100_000, total_assets=100_000,
                        heloc_balance=50_000, total_debt=50_000)]
        assert (MIN_AFTER_TAX_ESTATE.evaluate(indebted, {})
                != pytest.approx(-MAX_AFTER_TAX_ESTATE.evaluate(indebted, {})))

    def test_mirror_diverges_when_residence_equity_is_present(self):
        homed = [_yr(total_tfsa=100_000, total_assets=100_000)]
        cfg = {'property': {'house_value': 400_000.0}}
        assert (MIN_AFTER_TAX_ESTATE.evaluate(homed, cfg)
                != pytest.approx(-MAX_AFTER_TAX_ESTATE.evaluate(homed, cfg)))


# ============================================================================
# The golden invariant is byte-exact (the diff touches no fold file).
# ============================================================================

class TestGoldenHouseholdIsByteExact:
    """``drawable_after_tax`` is a read-only derived property and the score
    lives in the optimization layer; the simulation fold is untouched, so no
    pre-existing trajectory invariant can have moved -- asserted empirically
    against the canonical golden constant anyway (AGENTS.md)."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        rs = _run(golden_household_config())
        assert rs[-1].total_assets == 9709753.139463063, (
            f"golden terminal total_assets {rs[-1].total_assets!r} moved -- "
            f"the #1081 fix must not touch the fold")


if __name__ == '__main__':
    unittest.main()
