"""Issue #1009: the decumulation engine cannot spend a household down to a
spending target ("die with zero").

Two gaps, each tested here:

1. **The drawdown strands residual drawable financial wealth.** Once the
   accounts named in the configured ``drawdown_order`` are exhausted, the
   drawdown delivers ``$0/yr`` for the rest of the horizon while the residual
   drawable balances (a LIF that the configured order never named, an FHSA,
   ...) sit untouched and compound, and a full shortfall is reported every
   year. The reproduction (fabricated, round numbers, role-based names --
   DP#4/DP#15) gives the golden household a LIRA that converts to a LIF at 71
   and a spending target high enough that the usual accounts (TFSA/non-reg/
   RRSP) genuinely EXHAUST: with ``retirement.liquidate_to_target`` OFF the LIF
   compounds untouched while ``net_delivered`` falls to ``$0``; with the flag
   ON the drawdown liquidates the LIF to meet the target (respecting the LIF
   statutory maximum) until savings are genuinely exhausted.
   ``first_shortfall_year`` must then mean "financial savings genuinely
   exhausted", not "the usual accounts emptied while a LIF sits and compounds".

   Note on the threshold (#1008): before #1008 a $200k target stranded the
   LIF, because the discretionary drawdown sized to the full net target while
   the forced RRIF minimum was separately reinvested. #1008 nets the RRIF
   minimum's after-tax into the spending target, so the RRSP/RRIF funds more
   of it and the LIF only strands once the registered/TFSA/non-reg accounts
   are truly drained -- here at a $400k target. The strand itself is
   UNCHANGED by #1008; #1008 raised only the threshold at which it manifests,
   not the root cause (a drawable FINANCIAL account the configured
   ``drawdown_order`` never names). The liquidate-to-target residual sweep is
   therefore still needed. The no-op golden proof below is STRUCTURAL (a
   full-trajectory byte comparison, no hardcoded magic constant), so it stays
   correct when the golden invariant moves under an unrelated fix (#1008
   moved it to 9709753.139463063).

2. **No objective targets a near-zero estate.** Every wealth/estate objective
   MAXIMIZES what is left. This adds ``min_after_tax_estate`` -- the mirror of
   ``max_after_tax_estate`` -- so a household can optimise toward the smallest
   terminal estate ("die with ≈$0"). Registered + resolvable exactly like the
   existing objectives.

Scope notes (follow-ups, NOT this PR):
  - Full TWO-SPOUSE decumulation (the non-primary adult's own LIRA/LIF/FHSA
    slot-1 balances are not yet read into the per-year WorkingState scalars --
    cf. #901). This PR liquidates every drawable financial account the
    drawdown-order machinery already prices (slot-0 LIF/FHSA + both spouses'
    TFSA/RRSP/non-reg); the spouse's slot-1 locked-in/TFSA balances remain
    #901's territory.
  - A max-sustainable-spend / earliest-feasible-retirement SOLVER is a
    follow-up: with the liquidate-to-target fix the frontier is already
    discoverable by sweeping ``assumptions.retirement.spending_target`` and
    reading ``first_shortfall_year``.
  - #1002 (LIF max ceiling on the *discretionary* draw) stays open; the
    residual sweep introduced here respects the LIF statutory maximum for the
    NEW path only, so it neither duplicates nor re-breaks #1002.
"""

import os
import sys
import math
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig, YearResult

from test_golden_trajectory_581 import (
    golden_household_config, _run, START_YEAR, PRIMARY_BIRTH,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _config_with_lira_and_target(spending_target=400_000, liquidate=False):
    """The golden household + a primary-owned LIRA (slot 0) that converts to a
    LIF at 71, with a spending target high enough that the usual
    TFSA/non-reg/RRSP accounts (and their #1008 RRIF-minimum netting) genuinely
    EXHAUST, leaving the LIF as the residual drawable financial wealth the
    configured default order (``['tfsa','non_reg','rrsp']``) never names.
    Fabricated round numbers, role-based names (DP#4/DP#15).

    Note on the threshold: before #1008 a $200k target stranded the LIF,
    because the discretionary drawdown sized to the full net target while the
    forced RRIF minimum was separately reinvested. #1008 nets the RRIF minimum's
    after-tax into the spending target, so the RRSP/RRIF funds more of it and
    the LIF only strands once the registered/TFSA/non-reg accounts are truly
    drained -- here at a $400k target (verified: 25 stranded years where the
    drawdown delivers $0 while the LIF holds ~$1.3-2.0M). The strand itself is
    unchanged; #1008 only raised the threshold at which it manifests."""
    cfg = golden_household_config()
    cfg['lira'] = {
        'balance': 600_000, 'birth_year': PRIMARY_BIRTH,
        'jurisdiction': 'federal',
    }
    cfg['retirement']['spending_target'] = spending_target
    if liquidate:
        cfg['retirement']['liquidate_to_target'] = True
    return cfg


def _late_decumulation_years(rs):
    """Retirement years where the usual accounts (TFSA/non-reg/RRSP) are
    drained to ~0 -- the years the strand manifests in. These are the years
    where, pre-fix, ``net_delivered`` collapsed to ``$0`` while the LIF
    compounded."""
    return [
        r for r in rs
        if r.drawdown_net_target > 0
        and r.total_tfsa <= 1.0
        and r.non_reg_balance <= 1.0
        and r.total_rrsp <= 1.0
    ]


# ============================================================================
# Gap 1 — reproduction: the drawdown strands a drawable LIF.
# ============================================================================

class TestDrawdownStrandsResidualWealth:
    """The bug: a drawable LIF the configured order never named compounds
    untouched while the drawdown delivers $0 against a live spending target."""

    def test_reproduces_the_strand_when_liquidate_is_off(self):
        """SANITY: with the flag OFF, the LIF really does strand -- this is
        the pre-fix behaviour the issue reports. Documents the reproduction so
        the fix's assertion below is meaningful (the test file is self-
        proving: the OFF path shows the bug, the ON path shows the fix)."""
        rs = _run(_config_with_lira_and_target(liquidate=False))
        late = _late_decumulation_years(rs)
        assert len(late) > 0, "expected late-decumulation years where the " \
            "usual accounts are drained but the LIF still holds a balance"
        # In those years the LIF held a meaningful balance ...
        assert any(r.lif_balance > 10_000 for r in late)
        # ... yet the drawdown delivered $0 (or near-0) against a live target
        # ... the strand: wealth present, nothing drawn.
        assert any(r.drawdown_net_delivered < 1.0
                   and r.drawdown_net_target > 10_000
                   and r.lif_balance > 10_000
                   for r in late)

    def test_liquidate_to_target_drains_the_lif_instead_of_stranding_it(self):
        """The fix: with ``liquidate_to_target`` ON, the drawdown liquidates the
        LIF to meet the net target instead of delivering $0 while it compounds.
        The LIF is drawn down (respecting the LIF statutory maximum on the
        federal path -- ``net_delivered`` may sit below the target in a given
        year but is never $0 while a LIF balance remains), declines toward
        zero, and the terminal drops to near-zero (die-with-zero) rather than
        the stranded compounding LIF. No late year has a drawable LIF balance
        remaining while the drawdown delivered $0."""
        rs = _run(_config_with_lira_and_target(liquidate=True))
        late = _late_decumulation_years(rs)
        assert len(late) > 0
        # No late year delivers $0 while a drawable LIF balance remains --
        # the drawdown now liquidates it (capped only by the LIF statutory
        # maximum, which still draws > 0) rather than stranding it.
        for r in late:
            if r.lif_balance > 1.0:
                assert r.drawdown_net_delivered > 1.0, (
                    f"year {r.year}: LIF balance {r.lif_balance:.0f} remained "
                    f"but drawdown delivered {r.drawdown_net_delivered:.0f} "
                    f"against target {r.drawdown_net_target:.0f} -- residual "
                    f"wealth stranded (the #1009 bug)")
        # The LIF is actually drawn down (not left to compound untouched):
        # its terminal value is far below its peak. Pre-fix the LIF barely
        # moved from its conversion balance; post-fix it is liquidated to ~0.
        lif_peak = max(r.lif_balance for r in rs)
        lif_terminal = rs[-1].lif_balance
        assert lif_terminal < lif_peak * 0.5, (
            f"LIF terminal {lif_terminal:.0f} not meaningfully drawn down "
            f"from peak {lif_peak:.0f} -- liquidate_to_target did not liquidate")
        # Die-with-zero: the liquidate path spends the LIF down rather than
        # stranding it, so the terminal is far below the OFF-path terminal
        # (which strands ~$1.25M of LIF). Assert against the OFF run, not a
        # hardcoded constant, so the test stays correct if the golden fixture
        # or return model shifts.
        rs_off = _run(_config_with_lira_and_target(liquidate=False))
        assert rs[-1].total_assets < rs_off[-1].total_assets * 0.25, (
            f"ON terminal {rs[-1].total_assets:.0f} not meaningfully below OFF "
            f"terminal {rs_off[-1].total_assets:.0f} -- the LIF was not spent "
            f"down to near-zero (die-with-zero not achieved)")

    def test_liquidate_keeps_the_golden_household_byte_identical(self):
        """DP#32: absent/unused => byte-identical. The unmodified golden household
        has ZERO LIRA/LIF/FHSA balance throughout, so the liquidate-to-target
        residual sweep is a structural no-op for it whether the flag is absent,
        explicitly False, or True. Proven by a full-trajectory byte comparison
        against the live-computed golden -- NOT a hardcoded magic constant, so
        the test stays correct when the golden invariant moves under an
        unrelated fix (e.g. #1008 moved it to 9709753.139463063)."""
        import copy
        base = golden_household_config()
        off = copy.deepcopy(base)
        off['retirement']['liquidate_to_target'] = False
        on = copy.deepcopy(base)
        on['retirement']['liquidate_to_target'] = True
        rs_absent = _run(base)
        rs_off = _run(off)
        rs_on = _run(on)
        # Absent vs explicit False: the absence-handling is a true no-op.
        assert len(rs_absent) == len(rs_off)
        assert all(a.total_assets == b.total_assets
                   for a, b in zip(rs_absent, rs_off))
        # Absent vs True: the golden has no LIF/FHSA to sweep, so the residual
        # sweep is a no-op even with the flag on -- the feature cannot perturb a
        # household with no residual drawable financial wealth.
        assert len(rs_absent) == len(rs_on)
        assert all(a.total_assets == b.total_assets
                   for a, b in zip(rs_absent, rs_on))
        # Sanity: the golden terminal is a real, positive, finite estate.
        assert math.isfinite(rs_absent[-1].total_assets)
        assert rs_absent[-1].total_assets > 0


# ============================================================================
# Gap 2 — the min_after_tax_estate objective.
# ============================================================================

from objective import (
    OBJECTIVES, get_objective, ObjectiveFunction,
    MAX_AFTER_TAX_ESTATE, MIN_AFTER_TAX_ESTATE, compute_after_tax_estate,
)
import optimize


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


class TestMinAfterTaxEstateObjective(unittest.TestCase):
    """``min_after_tax_estate`` -- the mirror of ``max_after_tax_estate``: rank
    strategies toward the SMALLEST terminal after-tax estate ("die with
    ≈$0"). Registered + resolvable exactly like the existing objectives."""

    def test_is_registered_in_the_objectives_registry(self):
        self.assertIn('min_after_tax_estate', OBJECTIVES)
        self.assertIs(get_objective('min_after_tax_estate'),
                      MIN_AFTER_TAX_ESTATE)
        self.assertIsInstance(MIN_AFTER_TAX_ESTATE, ObjectiveFunction)

    def test_name_matches_the_registry_key(self):
        self.assertEqual(MIN_AFTER_TAX_ESTATE.name, 'min_after_tax_estate')

    def test_ranks_a_smaller_estate_above_a_larger_one(self):
        """Two fabricated terminal years: one leaves a $200k after-tax
        estate, the other $800k. ``min_after_tax_estate`` must score the
        smaller-estate strategy HIGHER (it is the closer-to-zero one) -- the
        mirror of ``max_after_tax_estate``, which scores the larger higher."""
        small = _yr(total_tfsa=200_000, total_assets=200_000)
        large = _yr(total_tfsa=800_000, total_assets=800_000)
        score_small = MIN_AFTER_TAX_ESTATE.evaluate([small], {})
        score_large = MIN_AFTER_TAX_ESTATE.evaluate([large], {})
        # Higher score == better (ObjectiveFunction.evaluate contract). A
        # smaller estate must rank above a larger one under the min objective.
        self.assertGreater(score_small, score_large,
                           "min_after_tax_estate must rank the smaller estate "
                           "higher (closer to die-with-zero)")
        # And it is exactly the mirror of max_after_tax_estate (negated):
        self.assertAlmostEqual(score_small,
                               -MAX_AFTER_TAX_ESTATE.evaluate([small], {}),
                               places=6)
        self.assertAlmostEqual(score_large,
                               -MAX_AFTER_TAX_ESTATE.evaluate([large], {}),
                               places=6)

    def test_resolvable_via_resolve_objective(self):
        """``optimize.resolve_objective`` resolves the name to the registered
        ObjectiveFunction (the --objective / decisions.objective path)."""
        obj = optimize.resolve_objective('min_after_tax_estate', {})
        self.assertIs(obj, MIN_AFTER_TAX_ESTATE)
        # An unknown name is still refused loudly (DP#32) -- the new objective
        # does not weaken the existing guard.
        with self.assertRaises(optimize.ObjectiveSelectionError):
            optimize.resolve_objective('min_bogus', {})

    def test_selects_the_die_with_zero_strategy_in_a_ranking(self):
        """A two-strategy ranking: a 'die-with-zero' strategy (small terminal
        estate) and a 'preserve-wealth' strategy (large terminal estate).
        Under ``min_after_tax_estate`` the die-with-zero strategy ranks first;
        under ``max_after_tax_estate`` the preserve-wealth one ranks first
        (the mirror)."""
        die_with_zero = [_yr(total_tfsa=10_000, total_assets=10_000)]
        preserve_wealth = [_yr(total_tfsa=900_000, total_assets=900_000)]
        ranked_min = sorted(
            [die_with_zero, preserve_wealth],
            key=lambda traj: MIN_AFTER_TAX_ESTATE.evaluate(traj, {}),
            reverse=True)
        self.assertIs(ranked_min[0], die_with_zero)
        ranked_max = sorted(
            [die_with_zero, preserve_wealth],
            key=lambda traj: MAX_AFTER_TAX_ESTATE.evaluate(traj, {}),
            reverse=True)
        self.assertIs(ranked_max[0], preserve_wealth)