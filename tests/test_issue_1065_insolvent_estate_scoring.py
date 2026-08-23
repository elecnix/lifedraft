"""Issue #1065 — ``min_after_tax_estate`` rewards dying INSOLVENT.

``countries/canada/estate.py`` ``gross_estate``/``net_estate`` subtract debts
with NO floor at zero, and ``objective._neg_after_tax_estate`` returns
``-net_estate`` under a "higher is better" contract. So a household that dies
INSOLVENT (financial assets spent to zero, debt still outstanding) gets a
POSITIVE score and outranks a household that dies cleanly at exactly $0 -- the
more insolvent the death, the higher the score, with no maximum. The
function's own docstring claimed the opposite ("a trajectory that already
spent down to zero scores 0.0 ... never a fabricated bonus"); that claim was
load-bearing and false once ``net_estate`` goes negative.

1. **Reachability** (Step 3 of the issue): the bug is **LIVE on main**, not
   latent. ``TestInsolvencyReachability::test_insolvent_die_with_zero_is_reachable_on_main_today``
   builds a schema-valid contract from the repo's OWN shipped helpers -- a
   $300k unsecured ``personal_loan`` (``_closed_end_liability``, #763, on
   main) whose amortization term outlives the 50-year projection so it never
   closes, plus a ``_add_principal_sale`` (#956/#964, on main) that discharges
   the mortgage + HELOC AND zeroes ``house_value`` in the estate
   (``objective.py``'s ``_estate_call_args``), plus ``liquidate_to_target``
   (#1009, on main) spending the drawable financial accounts to zero -- runs
   it through ``contract_schema.validate_contract`` -> ``to_internal_config`` -> the
   engine, and gets ``net_estate = -50,000`` (the example's $250k tax-free
   life-insurance death benefit less the $300k surviving personal loan). The
   PRE-fix ``min_after_tax_estate`` score is ``+50,000`` -- the fabricated
   bonus, live. No unmerged PR is required. The structural argument that
   ``house_equity`` covers the HELOC (OSFI B-20, ``heloc_within_revolving_limit``)
   covers ONLY the HELOC: ``SimState.total_debt()`` also sums
   ``consumer_loan_balances`` and ``credit_facility_balance`` (unsecured, no
   LTV bound), and a principal sale removes the house from the estate. The
   fix is therefore a **correction**, not a precaution. The SM/HELOC
   leveraged-drawdown path specifically stays solvent (the #1032 unwind repays
   the HELOC and OSFI B-20 bounds it below ``house_equity``), pinned by the
   sibling reachability tests -- but that is one path, not the whole space.

2. **The fix shape** (Step 4): three candidates were proposed -- ``-abs(net)``,
   signed-value-plus-an-explicit-surfaced-insolvency-penalty, or treat
   insolvency as INFEASIBLE (DP#32). This PR picks the SECOND (signed value +
   insolvency penalty) and justifies it against the other two in the PR body.
   The penalty is ``-|net_estate| - |net_estate| = -2|net_estate|`` for an
   insolvent trajectory, which MOVES (not eliminates) the tie that rejects
   ``-abs(net)``: an insolvency of $X ties a solvent surplus of $2X and
   OUTRANKS any solvent surplus above $2X. This meets the issue's stated
   acceptance (clean $0 strictly outranks any insolvency) and the issue
   sanctioned option (A), which has the same class of behaviour -- but the
   crossover is DISCLOSED here in the docstring, the PR body, and pinned by
   ``TestInsolvencyCrossover`` rather than hidden. The invariant-based tests
   below assert:

   - a trajectory ending at exactly $0 net estate ranks STRICTLY better than
     any trajectory ending insolvent, under ``min_after_tax_estate``;
   - a deeper insolvency ranks strictly worse than a shallower one (the
     gradient is preserved -- insolvency is priced, not hidden);
   - an insolvent trajectory ranks strictly worse than a solvent trajectory
     that left the SAME dollar amount on the table (insolvency is not
     symmetric with inefficiency -- a plan that ends owing money is worse than
     a plan that merely fails to spend it all);
   - the 2:1 crossover is pinned as an accepted, documented property;
   - ``max_after_tax_estate`` has no mirror issue: a negative estate already
     ranks low under a maximization objective (it returns ``net_estate``
     verbatim), and the tests pin that so a future refactor cannot silently
     turn a negative into a small positive.

Fabricated round numbers, role-based names (DP#4/DP#15). The D1 reachability
household is a real, schema-validated, engine-run contract -- not a synthetic
``YearResult`` -- so it settles the issue's acceptance criterion #2
(end-to-end). The synthetic ``YearResult`` cases exercise the objective's
arithmetic at the boundary (a terminal balance sheet with debt > assets and
no house) for the pricing invariants.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import logging

import pytest
from test_golden_trajectory_581 import _run, golden_household_config
from test_issue_763_consumer_loans import _closed_end_liability, _doc_with_liabilities
from test_issue_956_bite_e_principal_sale import _add_principal_sale
from test_issue_1009_die_with_zero import _yr
from test_issue_1017_sm_unwind import _run_with_sm_sleeve, _sm_unwind_config

import input_contract as ic
from countries.canada.estate import EstateResult
from objective import (
    MAX_AFTER_TAX_ESTATE,
    MIN_AFTER_TAX_ESTATE,
    compute_after_tax_estate,
)
from simulation import FamilySimulation
from simulation_config import SimulationConfig
import contract_schema

# ── Helpers ─────────────────────────────────────────────────────────────────

def _insolvent_year(debt=200_000):
    """A terminal year that is INSOLVENT: a $``debt`` non-mortgage debt
    outstanding, no financial assets, no house (so ``house_equity`` cannot
    cover the debt). This is the balance sheet the #1065 failure mode produces
    -- a die-with-zero household whose investments were spent to zero while a
    debt rode to death. Used to exercise the objective's ARITHMETIC at the
    boundary (the pricing invariants); the end-to-end reachability acceptance
    is ``_insolvent_die_with_zero_contract`` below."""
    return _yr(total_debt=debt, heloc_balance=debt)


def _insolvent_die_with_zero_contract(spending_target=400_000):
    """A schema-valid contract that TERMINATES INSOLVENT on main today (D1).

    Built from the repo's OWN shipped helpers -- no contortion of the contract
    into something no real household would declare:
      - a $300k unsecured ``personal_loan`` (``_closed_end_liability``, #763)
        with an interest-only payment ($1,250/mo @ 5% = $15k/yr = exactly the
        first-year interest) and an amortization term (60y) that OUTLIVES the
        50-year projection, so the ``consumer_loans`` rule never force-closes
        it and the $300k balance rides to death (an unsecured debt with no LTV
        bound -- the path the "OSFI B-20 keeps the estate solvent" argument
        does NOT cover);
      - a ``_add_principal_sale`` (#956/#964) dated 2031, which discharges the
        mortgage + HELOC AND removes the house from the estate
        (``objective._estate_call_args`` zeroes ``house_value`` on a principal
        sale), so there is no ``house_equity`` to cover the surviving loan;
      - ``liquidate_to_target`` (#1009) spending the drawable financial
        accounts to zero.
    The shipped example's $250k tax-free life-insurance death benefit is the
    only remaining asset, so ``gross_estate = 250k - 300k = -50k`` and
    ``net_estate = -50,000``. Fabricated round numbers, role-based names
    (DP#4/DP#15)."""
    personal = _closed_end_liability(
        "personal_loan", balance=300_000, rate=0.05,
        payment_monthly=1_250, years=60)  # interest-only; term > horizon -> persists
    doc = _doc_with_liabilities(personal, keep_mortgage=True)
    doc = _add_principal_sale(doc, {"year": 2031, "selling_costs": 30_000})
    doc.setdefault("assumptions", {}).setdefault("retirement", {})[
        "liquidate_to_target"] = True
    doc.setdefault("assumptions", {}).setdefault("retirement", {})[
        "spending_target"] = spending_target
    return doc


def _run_contract(doc):
    """Validate -> map to internal config -> run the real engine; return
    (results, internal_cfg)."""
    logging.disable(logging.WARNING)
    try:
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)
        return FamilySimulation(SimulationConfig.from_dict(cfg)).run(), cfg
    finally:
        logging.disable(logging.NOTSET)


# ============================================================================
# STEP 3 — reachability: can a run TERMINATE insolvent today?
# ============================================================================

class TestInsolvencyReachability:
    """Reachability, settled honestly. The bug is **LIVE on main**, not latent:
    ``test_insolvent_die_with_zero_is_reachable_on_main_today`` builds a
    schema-valid contract from the repo's own shipped helpers and terminates
    at ``net_estate = -50,000`` with a PRE-fix score of ``+50,000`` (the
    fabricated bonus). The fix is a correction. The SM/HELOC leveraged-
    drawdown path specifically stays solvent (the #1032 unwind + OSFI B-20
    bound on the HELOC), pinned by the sibling tests below -- but that is one
    path, not the whole space, because ``total_debt`` also sums unsecured
    consumer loans and a principal sale removes the house from the estate."""

    def test_insolvent_die_with_zero_is_reachable_on_main_today(self):
        """The issue's acceptance criterion #2: a real, schema-validated,
        engine-run household that terminates INSOLVENT, and the assertion that
        it does not outrank a clean $0. This is the D1 reproduction -- built
        from ``_closed_end_liability`` (#763) + ``_add_principal_sale``
        (#956/#964) + ``liquidate_to_target`` (#1009), all on main, no unmerged
        PR required. The PRE-fix ``-net_estate`` arithmetic scores it
        ``+50,000`` (the fabricated bonus, LIVE); the POST-fix penalty scores
        it ``-100,000`` (priced, below clean $0)."""
        rs, cfg = _run_contract(_insolvent_die_with_zero_contract())
        estate = compute_after_tax_estate(rs, cfg)
        # The engine genuinely terminates insolvent: the $300k unsecured
        # personal loan survives (it is not bounded by LTV and the principal
        # sale removed the house from the estate), liquidate_to_target spent
        # the financial assets to zero, and only the $250k life-insurance
        # death benefit remains -- gross_estate = 250k - 300k = -50k.
        assert estate.net_estate == -50_000.0, (
            f"net_estate {estate.net_estate:.2f} != -50,000 -- if the shipped "
            f"example's life-insurance / loan terms changed, update this pin "
            f"to the new value; the structural asserts below still hold")
        assert estate.insolvent
        assert estate.insolvency == 50_000.0
        # The bug, LIVE on main: the PRE-fix ``-net_estate`` arithmetic turns
        # the insolvency into a POSITIVE bonus that outranks a clean $0.
        pre_fix_score = -estate.net_estate
        assert pre_fix_score == 50_000.0, (
            f"PRE-fix score {pre_fix_score:.2f} != +50,000 -- the fabricated "
            f"bonus is not live; revisit the #1065 framing")
        assert pre_fix_score > 0.0  # the fabricated bonus
        # The fix: the insolvency is PRICED, scoring below the clean-$0 best.
        post_fix_score = MIN_AFTER_TAX_ESTATE.evaluate(rs, cfg)
        assert post_fix_score == -100_000.0, (
            f"POST-fix score {post_fix_score:.2f} != -100,000 (-2|net|)")
        assert post_fix_score < 0.0 < pre_fix_score  # corrected direction
        # Acceptance criterion #1: a clean $0 death strictly outranks this
        # insolvent trajectory under min_after_tax_estate.
        clean_zero_score = MIN_AFTER_TAX_ESTATE.evaluate([_yr()], {})
        assert clean_zero_score == 0.0
        assert clean_zero_score > post_fix_score

    def test_sm_sleeve_leveraged_die_with_zero_stays_solvent(self):
        """The SM/HELOC leveraged-drawdown path specifically stays solvent: the
        #1032 unwind repays the HELOC from the sleeve sale as the drawdown
        liquidates the sleeve, so the terminal estate is the house (excluded
        from drawdown) and the HELOC is retired to $0. This is ONE path -- not
        the whole space (the D1 test above shows a consumer-loan + principal-
        sale path that goes insolvent). Pinned so a future change that lets the
        SM HELOC outrun the sleeve / equity flips this red."""
        rs = _run_with_sm_sleeve(*_sm_unwind_config(
            spending_target=500_000, liquidate=True,
            sm_fmv=2_000_000, sm_acb=1_000_000, sm_heloc=200_000))
        cfg, _, _, _ = _sm_unwind_config(
            spending_target=500_000, liquidate=True,
            sm_fmv=2_000_000, sm_acb=1_000_000, sm_heloc=200_000)
        estate = compute_after_tax_estate(rs, cfg)
        assert rs[-1].sm_heloc_balance == 0.0  # the #1032 unwind repaid it
        assert rs[-1].sm_investment_balance == 0.0
        assert estate.net_estate > 0.0  # the house remains -> solvent
        assert not estate.insolvent

    def test_heloc_persists_when_liquidate_is_off_but_estate_stays_solvent(self):
        """With ``liquidate_to_target`` OFF, the SM sleeve strands and the
        HELOC rides to death (the #1017 pre-fix behaviour). The estate is
        STILL solvent: OSFI B-20 bounds the revolving HELOC at 65% LTV / 80%
        combined (``heloc_within_revolving_limit``, enforced every year), and
        the residence is excluded from drawdown, so ``house_equity`` exceeds
        the HELOC. 'Dies with an outstanding HELOC' and 'dies insolvent' are
        DIFFERENT states: the HELOC is covered by the house, an unsecured
        consumer loan (the D1 path) is not."""
        rs = _run_with_sm_sleeve(*_sm_unwind_config(
            spending_target=500_000, liquidate=False,
            sm_fmv=2_000_000, sm_acb=1_000_000, sm_heloc=200_000))
        cfg, _, _, _ = _sm_unwind_config(
            spending_target=500_000, liquidate=False,
            sm_fmv=2_000_000, sm_acb=1_000_000, sm_heloc=200_000)
        estate = compute_after_tax_estate(rs, cfg)
        assert rs[-1].sm_heloc_balance > 0.0  # the HELOC persists (unwind gated)
        # ... but the house covers it (OSFI B-20 bound) -- the SM HELOC path
        # specifically stays solvent. (An unsecured loan with no LTV bound,
        # as in the D1 test, is the path that does NOT stay solvent.)
        assert estate.house_equity > estate.debts
        assert estate.net_estate > 0.0
        assert not estate.insolvent


# ============================================================================
# STEP 4 — the fix: insolvency is priced, never a fabricated bonus.
# ============================================================================

class TestInsolventEstateIsPricedNotRewarded:
    """The acceptance criteria. Under ``min_after_tax_estate`` (higher score ==
    better), a trajectory ending at exactly $0 net estate must rank STRICTLY
    better than any trajectory ending insolvent, and a deeper insolvency must
    rank strictly worse than a shallower one. The score for an insolvent
    trajectory must be NEGATIVE (below clean $0's 0.0), never the POSITIVE
    fabricated bonus the unfixed ``-net_estate`` arithmetic produced."""

    def test_clean_zero_outranks_insolvent(self):
        """Acceptance: a trajectory ending at exactly $0 net estate ranks
        strictly better than a trajectory ending insolvent (an outstanding
        HELOC with the assets spent to zero)."""
        clean = [_yr()]                       # dies at exactly $0
        insolvent = [_insolvent_year(200_000)]  # dies owing $200k on a HELOC
        score_clean = MIN_AFTER_TAX_ESTATE.evaluate(clean, {})
        score_insolvent = MIN_AFTER_TAX_ESTATE.evaluate(insolvent, {})
        assert score_clean == 0.0
        # The fix's whole point: insolvency scores BELOW clean, not above.
        assert score_insolvent < score_clean, (
            f"insolvent score {score_insolvent:.2f} is not strictly below "
            f"clean $0 score {score_clean:.2f} -- insolvency is being "
            f"rewarded, not priced (the #1065 bug)")

    def test_insolvent_score_is_negative_never_a_fabricated_bonus(self):
        """The docstring's 'never a fabricated bonus' claim, made load-bearing.
        Pre-fix, an insolvent trajectory scored ``-net_estate = +|net_estate``
        -- a POSITIVE bonus that grew with the depth of insolvency. Post-fix,
        an insolvent trajectory scores NEGATIVE (below the clean-$0 score of
        0.0), so it can never outrank a clean death."""
        insolvent = [_insolvent_year(200_000)]
        score = MIN_AFTER_TAX_ESTATE.evaluate(insolvent, {})
        assert score < 0.0, (
            f"insolvent score {score:.2f} is not negative -- the objective "
            f"is still fabricating a bonus for dying in debt (the #1065 bug)")

    def test_deeper_insolvency_ranks_strictly_worse(self):
        """Insolvency is PRICED, not hidden: a trajectory dying owing $400k
        ranks strictly worse than one dying owing $100k. (Clamping ``net_estate``
        at 0 -- the option the issue forbids -- would make both score 0 and
        collapse this gradient; the fix preserves it.)"""
        shallow = [_insolvent_year(100_000)]
        deep = [_insolvent_year(400_000)]
        score_shallow = MIN_AFTER_TAX_ESTATE.evaluate(shallow, {})
        score_deep = MIN_AFTER_TAX_ESTATE.evaluate(deep, {})
        assert score_deep < score_shallow < 0.0, (
            f"deep insolvency {score_deep:.2f} is not strictly worse than "
            f"shallow insolvency {score_shallow:.2f} -- the depth gradient "
            f"is lost (insolvency is hidden, not priced)")

    def test_insolvent_ranks_worse_than_leaving_the_same_amount_on_the_table(self):
        """Insolvency is not symmetric with inefficiency. A trajectory that
        DIES OWING $200k is strictly worse than one that dies WITH $200k left
        over -- a plan that ends in debt is not a plan, while a plan that
        merely fails to spend its last $200k is inefficient but solvent. This
        is the asymmetry that distinguishes the chosen fix (signed value + an
        explicit insolvency penalty) from ``-abs(net_estate)``, which would
        TIE the two (both $200k from zero) and hide the insolvency."""
        left_over = [_yr(total_tfsa=200_000, total_assets=200_000)]  # +$200k
        insolvent = [_insolvent_year(200_000)]                       # -$200k
        score_left = MIN_AFTER_TAX_ESTATE.evaluate(left_over, {})
        score_insolvent = MIN_AFTER_TAX_ESTATE.evaluate(insolvent, {})
        # Both are "missed die-with-zero by $200k", but the insolvent miss is
        # strictly worse -- the penalty the fix adds on top of the symmetric
        # distance-from-zero.
        assert score_insolvent < score_left < 0.0, (
            f"insolvent {score_insolvent:.2f} is not strictly worse than "
            f"left-over {score_left:.2f} -- insolvency is being treated as "
            f"symmetric with inefficiency (the -abs(net) shape, rejected)")

    def test_solvent_ranking_is_unchanged_closer_to_zero_still_better(self):
        """The fix is a no-op for SOLVENT trajectories (``net_estate >= 0``):
        a smaller positive estate still ranks above a larger one (closer to
        die-with-zero), exactly as before. The correction only changes the
        insolvent branch (the live bug's territory), so every solvent
        household -- incl. the golden household -- ranks byte-identically, and
        the golden household stays byte-exact (see below)."""
        small = [_yr(total_tfsa=10_000, total_assets=10_000)]
        large = [_yr(total_tfsa=900_000, total_assets=900_000)]
        # Both solvent -> scores are negative (-10k, -900k); closer-to-zero
        # ranks higher (small -10k > large -900k), exactly as before the fix.
        assert (MIN_AFTER_TAX_ESTATE.evaluate(small, {})
                > MIN_AFTER_TAX_ESTATE.evaluate(large, {}))
        assert MIN_AFTER_TAX_ESTATE.evaluate(small, {}) < 0.0  # solvent surplus

    def test_clean_zero_is_the_unique_best(self):
        """Under ``min_after_tax_estate``, a clean $0 death is the unique best
        achievable score (0.0) -- no solvent surplus beats it and no insolvency
        ties it. This is the docstring's 'best achievable' claim, made true."""
        clean = [_yr()]
        surplus = [_yr(total_tfsa=500_000, total_assets=500_000)]
        insolvent = [_insolvent_year(1)]  # even $1 insolvent
        best = MIN_AFTER_TAX_ESTATE.evaluate(clean, {})
        assert best == 0.0
        assert MIN_AFTER_TAX_ESTATE.evaluate(surplus, {}) < best
        assert MIN_AFTER_TAX_ESTATE.evaluate(insolvent, {}) < best


# ============================================================================
# D3 — the 2:1 crossover, pinned as an accepted, documented property.
# ============================================================================

class TestInsolvencyCrossover:
    """The fix's shape is ``-net_estate`` for ``net_estate >= 0`` and
    ``-2|net_estate|`` for ``net_estate < 0``. This MOVES (not eliminates) the
    tie that rejects ``-abs(net_estate)``: an insolvency of $X ties a solvent
    surplus of $2X and OUTRANKS any solvent surplus above $2X. This meets the
    issue's stated acceptance (clean $0 strictly outranks any insolvency --
    0.0 > -2|net|) and the issue sanctioned option (A), which has the same
    class of behaviour; the crossover is DISCLOSED here, in the objective
    docstring, and in the PR body rather than hidden. These tests pin it as an
    ACCEPTED property so a future change to the shape (e.g. switching to
    option C, infeasibility) is a deliberate, visible decision, not a silent
    drift."""

    def test_insolvency_of_x_ties_solvent_surplus_of_2x(self):
        """Insolvency of $200k scores -400k; a solvent surplus of $400k also
        scores -400k. The 2:1 ratio: the insolvency penalty doubles the
        symmetric distance-from-zero, so insolvency-of-X lands where
        surplus-of-2X lands."""
        insolvent_200k = [_insolvent_year(200_000)]   # net = -200k -> -400k
        surplus_400k = [_yr(total_tfsa=400_000, total_assets=400_000)]  # +400k -> -400k
        assert (MIN_AFTER_TAX_ESTATE.evaluate(insolvent_200k, {})
                == pytest.approx(MIN_AFTER_TAX_ESTATE.evaluate(surplus_400k, {})))
        assert MIN_AFTER_TAX_ESTATE.evaluate(insolvent_200k, {}) == -400_000.0

    def test_insolvency_of_x_outranks_solvent_surplus_above_2x(self):
        """The accepted inversion: an insolvent trajectory ($50k underwater)
        OUTRANKS a solvent trajectory that leaves a LARGE surplus ($600k)
        because -2*50k = -100k > -600k. This is the property the issue's
        option (A) also has; the fix chose (B) (disclosed) over (A) (same
        behaviour, hidden) and over (C) (infeasibility, which would refuse
        the insolvent trajectory outright). Pinned so the choice is visible."""
        insolvent_50k = [_insolvent_year(50_000)]   # -100k
        surplus_600k = [_yr(total_tfsa=600_000, total_assets=600_000)]  # -600k
        score_insolvent = MIN_AFTER_TAX_ESTATE.evaluate(insolvent_50k, {})
        score_surplus = MIN_AFTER_TAX_ESTATE.evaluate(surplus_600k, {})
        assert score_insolvent == -100_000.0
        assert score_surplus == -600_000.0
        assert score_surplus < 0.0  # a solvent surplus still scores below clean $0
        # The accepted inversion: the insolvent trajectory outranks the
        # large-surplus solvent trajectory (-100k > -600k).
        assert score_insolvent > score_surplus

    def test_clean_zero_still_strictly_outranks_any_insolvency(self):
        """The issue's acceptance criterion #1, holding DESPITE the crossover:
        clean $0 (0.0) strictly outranks every insolvent trajectory (-2|net| <
        0), including a $1 insolvency. The crossover only lets insolvency
        outrank LARGE solvent SURPLUSES, never a clean $0."""
        clean = MIN_AFTER_TAX_ESTATE.evaluate([_yr()], {})
        assert clean == 0.0
        for debt in (1.0, 50_000.0, 1_000_000.0):
            assert clean > MIN_AFTER_TAX_ESTATE.evaluate([_insolvent_year(debt)], {})



# ============================================================================
# The insolvency is SURFACED on the EstateResult (DP#32 / #585).
# ============================================================================

class TestInsolvencyIsSurfaced:
    """The insolvency is surfaced as NAMED FIELDS on ``EstateResult``
    (``insolvent`` / ``insolvency``) so the objective prices it from a named
    field rather than magic arithmetic, and so the EstateResult carries the
    fact rather than laundering the negative through the score alone. The
    objective's ``_neg_after_tax_estate`` reads ``estate.insolvency`` to price
    the penalty; ``insolvent`` is the predicate ``insolvency`` is built on.
    (An output render that DISCLOSES the insolvency to the user is a follow-up
    -- tracked as review item D5 -- not claimed here.)"""

    def test_solvent_estate_is_not_insolvent(self):
        estate = compute_after_tax_estate(
            [_yr(total_tfsa=100_000, total_assets=100_000)], {})
        assert not estate.insolvent
        assert estate.insolvency == 0.0

    def test_clean_zero_estate_is_not_insolvent(self):
        estate = compute_after_tax_estate([_yr()], {})
        assert not estate.insolvent
        assert estate.insolvency == 0.0

    def test_insolvent_estate_is_flagged_with_its_depth(self):
        estate = compute_after_tax_estate([_insolvent_year(200_000)], {})
        assert estate.insolvent
        assert estate.insolvency == pytest.approx(200_000.0)
        assert estate.net_estate == pytest.approx(-200_000.0)

    def test_empty_estate_result_is_solvent_by_default(self):
        """An empty ``EstateResult`` (no results) is solvent at $0 -- a
        modelled zero, never a fabricated insolvency (DP#32)."""
        empty = EstateResult()
        assert not empty.insolvent
        assert empty.insolvency == 0.0
        assert empty.net_estate == 0.0


# ============================================================================
# Mirror check: max_after_tax_estate does not treat a negative as a small
# positive anywhere (issue acceptance: "check max_after_tax_estate for the
# mirror issue").
# ============================================================================

class TestMaxAfterTaxEstateMirror:
    """``max_after_tax_estate`` MAXIMIZES ``net_estate`` verbatim (``_after_tax_
    estate`` returns ``compute_after_tax_estate(...).net_estate`` with no
    transformation). A negative estate is therefore already ranked LOW under a
    maximization objective (``-200k < 0``), so there is no mirror bug -- but
    the issue asks us to CHECK, so these tests pin the correct behaviour so a
    future refactor (e.g. a ``max(0, net)`` clamp "to avoid a confusing
    negative") cannot silently turn a negative estate into a small positive
    one anywhere."""

    def test_max_ranks_clean_zero_above_insolvent(self):
        """Under ``max_after_tax_estate``, a clean $0 estate (0.0) outranks an
        insolvent one (-200k): a negative is a negative, not a small positive."""
        clean = [_yr()]
        insolvent = [_insolvent_year(200_000)]
        assert MAX_AFTER_TAX_ESTATE.evaluate(clean, {}) == 0.0
        assert MAX_AFTER_TAX_ESTATE.evaluate(insolvent, {}) < 0.0
        assert (MAX_AFTER_TAX_ESTATE.evaluate(clean, {})
                > MAX_AFTER_TAX_ESTATE.evaluate(insolvent, {}))

    def test_max_does_not_clamp_a_negative_estate_to_zero(self):
        """The mirror of the #1065 forbidden clamp: ``max_after_tax_estate``
        must NOT clamp a negative ``net_estate`` up to 0 (which would make
        'dies owing $200k' score identically to 'dies clean' under the max
        objective too). It returns the negative verbatim."""
        insolvent = [_insolvent_year(200_000)]
        score = MAX_AFTER_TAX_ESTATE.evaluate(insolvent, {})
        assert score == pytest.approx(-200_000.0), (
            f"max_after_tax_estate returned {score:.2f} for an insolvent "
            f"estate -- a negative must be passed through verbatim, not "
            f"clamped to 0 (the mirror of the #1065 clamp)")

    def test_max_deeper_insolvency_ranks_strictly_worse(self):
        """Under ``max_after_tax_estate`` too, deeper insolvency is strictly
        worse (the negative grows), so the depth gradient is preserved on both
        sides of the mirror."""
        shallow = [_insolvent_year(100_000)]
        deep = [_insolvent_year(400_000)]
        assert (MAX_AFTER_TAX_ESTATE.evaluate(deep, {})
                < MAX_AFTER_TAX_ESTATE.evaluate(shallow, {})
                < 0.0)


# ============================================================================
# Golden household: byte-exact. The fix is a no-op for solvent trajectories.
# ============================================================================

class TestGoldenHouseholdIsByteExact:
    """The golden household is solvent (``net_estate > 0``), so the fix's
    insolvent branch never fires for it -- its ``min_after_tax_estate`` score
    is byte-identical to pre-fix, and the engine output is untouched. The fix
    touches only ``objective.py`` (the optimization layer) and adds two
    read-only ``@property`` declarations to ``countries/canada/estate.py``
    (``insolvent`` / ``insolvency`` -- no change to ``gross_estate`` /
    ``net_estate`` / ``total_tax`` arithmetic). The simulation fold
    (``simulation_rules.py`` / ``simulation.py`` / ``simulation_state.py``) is
    NOT in the diff, so the terminal ``total_assets`` -- a fold output --
    cannot have moved by construction; the byte-exact assertion below verifies
    it empirically too."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        rs = _run(golden_household_config())
        # The canonical golden on `main` at the time of this PR (moved by #1046,
        # which wired the RESP annual allocation + RESPChild lifetime state --
        # see AGENTS.md `The golden invariant`). Pinned so the #1065 fix, which
        # touches no engine file, is proven not to move it; if `main` moves the
        # golden again under this branch, update this constant in the SAME PR
        # (the byte-exact guard is structural, not a magic number).
        assert rs[-1].total_assets == 9709753.139463063, (
            f"golden terminal total_assets {rs[-1].total_assets!r} != "
            f"9709753.139463063 -- the #1065 fix must not move the golden "
            f"household (the diff touches no engine file)")

    def test_golden_min_after_tax_estate_score_is_unchanged(self):
        """The golden household is solvent, so ``min_after_tax_estate`` returns
        ``-net_estate`` exactly as before the fix -- the insolvency penalty
        branch is inert for every reachable household today."""
        cfg = golden_household_config()
        rs = _run(cfg)
        estate = compute_after_tax_estate(rs, cfg)
        assert estate.net_estate > 0.0  # solvent -> the fix is a no-op
        assert not estate.insolvent
        score = MIN_AFTER_TAX_ESTATE.evaluate(rs, cfg)
        # Pre-fix and post-fix both return -net_estate for a solvent estate.
        assert score == pytest.approx(-estate.net_estate)

    def test_golden_estate_result_is_not_insolvent(self):
        cfg = golden_household_config()
        rs = _run(cfg)
        estate = compute_after_tax_estate(rs, cfg)
        assert not estate.insolvent
        assert estate.insolvency == 0.0


if __name__ == '__main__':
    unittest.main()
