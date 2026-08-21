"""Hand-computable drawdown oracle fixtures — PR 0 of the progressive-drawdown series.

Issues #363 / #618 / #579.

WHY THIS FILE EXISTS
--------------------
The retirement drawdown prices *every* taxable dollar at ONE flat marginal
rate. The rate is computed once, in ``apply_retirement_income``
(``simulation_rules.py`` ~line 855):

    draw_rate = marginal_rate(cpp + pension + net_shortfall, year_brackets)

and then handed to ``plan_drawdown_net``
(``countries/canada/retirement_transition.py``), which grosses up a fully
taxable RRSP/RRIF withdrawal at ``net_per = 1 - rate`` — a single flat rate
for the whole draw. That flat rate:

  (a) does NOT re-bracket the incremental draw as it climbs through the tax
      table (a $60k draw that crosses two bracket boundaries is priced at the
      top rate for every dollar), so the engine OVER-draws;
  (b) does NOT fold in the OAS 15 % recovery tax (clawback), so in the
      clawback zone each RRSP dollar silently costs 15 cents more than the
      engine charges, and the engine UNDER-delivers the requested net; and
  (c) does NOT split the draw across two spouses' separate bracket sets
      (RETIRED by #363 PR 4 — see ``DrawdownTwoSpouseSplit`` below).

This module is a TEST-ONLY, BEHAVIOR-PRESERVING foundation. It does not fix
any of that. It pins today's behaviour AND commits a hand-computed, CRA-style
"oracle" — the arithmetic a progressive/clawback-aware engine must produce —
so the follow-on PRs have something concrete and independently reviewable to
flip:

  * PR 1 (progressive re-bracketing) flipped ``test_fixture1_progressive_gross``
    to green — the engine now re-brackets the taxable draw.
  * PR 2 (OAS clawback folded into the drawdown) flipped
    ``test_fixture2_clawback_aware_net`` to green, and added the fixpoint
    determinism/monotonicity coverage (``DrawdownOASClawbackFixpoint``).
  * PR 4 (per-spouse split) added ``DrawdownTwoSpouseSplit``: the couple's draw
    is priced against the two spouses' SEPARATE bracket sets (Canada has no
    joint filing), so household tax is the SUM of each spouse's own progressive
    tax and is <= the single-combined-schedule draw.

The oracle is derived from the pure, already-shipped ``tax_calculator``
primitives (``tax_on_income``, ``marginal_rate``) and the pure
``countries.canada.retirement`` clawback primitive (``oas_clawback``) — the
same building blocks the working phase uses (``simulation.py`` ~line 545). It
is NOT copied from engine output: a reader can see "this is the arithmetic a
CRA calculator produces" line by line.

DP#15/#4: every figure below is FABRICATED and ROUND; roles are generic
("single retiree"). Nothing here is a real person's finances.
"""

from tax_data import default_tax_provider
import unittest

from tax_calculator import tax_on_income, marginal_rate
from countries.canada.retirement import (
    oas_clawback, get_oas_annual_max, get_oas_clawback_threshold,
)
import countries.canada.retirement_transition as rt
from countries.canada.retirement_transition import plan_drawdown_net


# The simulation's default tax year / province (see get_combined_brackets
# defaults and the golden household). DP#20: year-versioned brackets.
YEAR = 2026
PROVINCE = "quebec"
BRACKETS = default_tax_provider().get_combined_brackets(YEAR, PROVINCE)

# A comfortably large RRSP balance so the draw is never balance-limited —
# these fixtures are about how the draw is *priced*, not about running dry.
BIG_RRSP = 1_000_000.0


def _flat_engine_rate(other_taxable_income, net_shortfall):
    """Reproduce the engine's ONE-LINE flat-rate formula verbatim.

    ``apply_retirement_income`` computes exactly this (simulation_rules.py
    ~855): ``marginal_rate(cpp + pension + net_shortfall, year_brackets)``,
    then stores it as ``ws.retiree_marginal_rate`` and passes it to
    ``plan_drawdown_net``. We call the SAME public primitive with the SAME
    argument so the flat rate driving ``plan_drawdown_net`` below is the real
    engine rate, not a re-derivation of it. ``other_taxable_income`` here is
    ``cpp + pension``.
    """
    return marginal_rate(other_taxable_income + net_shortfall, BRACKETS)


class DrawdownProgressiveOracle(unittest.TestCase):
    """Fixture 1 — a single retiree whose RRSP draw spans >= 2 brackets.

    Fabricated (DP#15/#4): CPP $15,000 + pension $10,000 = $25,000 of other
    taxable income. A ~$60,000 fully-taxable RRSP draw takes taxable income
    from $25,000 to $85,000, crossing the $54,345 and $58,523 combined
    federal+provincial boundaries — two boundaries, as required.
    """

    CPP = 15_000.0
    PENSION = 10_000.0
    OTHER_TAXABLE = CPP + PENSION            # $25,000 base income
    GROSS_ORACLE = 60_000.0                  # the round draw the oracle prices

    # --- The hand-computed CRA oracle (progressive), derived from primitives.
    # tax on the draw = tax_on_income(base + draw) - tax_on_income(base).
    BASE_TAX = tax_on_income(OTHER_TAXABLE, BRACKETS)
    TOP_TAX = tax_on_income(OTHER_TAXABLE + GROSS_ORACLE, BRACKETS)
    PROGRESSIVE_TAX = TOP_TAX - BASE_TAX
    # Net a $60,000 progressive draw actually delivers to the household.
    PROGRESSIVE_NET = GROSS_ORACLE - PROGRESSIVE_TAX

    def test_fixture_spans_at_least_two_bracket_boundaries(self):
        """Guard the fixture's premise: the draw crosses >= 2 boundaries.

        If a future bracket re-index moved the table, this fails loudly rather
        than letting fixtures 1/2 silently stop exercising the multi-bracket
        path they exist to characterize.
        """
        lo = self.OTHER_TAXABLE
        hi = self.OTHER_TAXABLE + self.GROSS_ORACLE
        crossed = [b['min'] for b in BRACKETS if lo < b['min'] < hi]
        self.assertGreaterEqual(
            len(crossed), 2,
            f"draw from {lo} to {hi} must cross >= 2 boundaries; crossed {crossed}")

    def test_current_engine_prices_the_whole_draw_at_the_flat_top_rate(self):
        """TODAY'S BEHAVIOUR (green): flat-rate gross-up, no re-bracketing.

        Drive the REAL drawdown (``plan_drawdown_net``) with a net need equal
        to the net a correct $60k progressive draw would deliver
        (PROGRESSIVE_NET). Because the engine grosses up at the single flat
        top rate, it draws MORE than $60,000 to deliver that same net.
        """
        net_need = self.PROGRESSIVE_NET
        flat_rate = _flat_engine_rate(self.OTHER_TAXABLE, net_need)

        plan = plan_drawdown_net(
            net_need, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=flat_rate,
        )

        # The flat top rate for this fixture is the third bracket's 0.3612.
        self.assertEqual(flat_rate, 0.3612)
        # Fully-taxable RRSP: gross = net_need / (1 - flat_rate), all taxable,
        # net delivered == the requested net (accounts had the balance).
        expected_flat_gross = net_need / (1 - flat_rate)
        self.assertAlmostEqual(plan.total_withdrawn, expected_flat_gross, places=6)
        self.assertAlmostEqual(plan.taxable_withdrawn, expected_flat_gross, places=6)
        self.assertAlmostEqual(plan.net_delivered, net_need, places=6)

        # The DEFECT, quantified: the flat-rate engine over-draws vs the
        # progressive oracle (it drew ~$5,146 more RRSP than a CRA-correct
        # re-bracketing needs to deliver the identical net).
        self.assertGreater(plan.total_withdrawn, self.GROSS_ORACLE)
        self.assertAlmostEqual(
            plan.total_withdrawn - self.GROSS_ORACLE, 5_146.44474, places=4)

    def test_fixture1_progressive_gross(self):
        """PR 1 (#363) landed: progressive re-bracketing (was expectedFailure).

        A progressive, re-bracketing drawdown, asked for exactly the net that
        a $60,000 draw delivers under CRA-style progressive tax, must draw
        exactly $60,000 — not the flat-rate engine's ~$65,146. PR 1 flipped
        this from an expected failure to green by passing the year-versioned
        ``brackets`` (and the base ``other_taxable_income``) to
        ``plan_drawdown_net``.

        The deprecated scalar ``marginal_rate`` is still passed but MUST be
        ignored when ``brackets`` is present. Two independent computations of
        the tax — the engine's closed-form bracket walk and an in-test
        ``tax_on_income`` delta — must agree to the cent.
        """
        net_need = self.PROGRESSIVE_NET
        flat_rate = _flat_engine_rate(self.OTHER_TAXABLE, net_need)
        plan = plan_drawdown_net(
            net_need, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=flat_rate,          # deprecated fallback — ignored here
            other_taxable_income=self.OTHER_TAXABLE,
            brackets=BRACKETS,
        )
        # Progressive truth: gross == GROSS_ORACLE, taxable == GROSS_ORACLE,
        # tax == PROGRESSIVE_TAX (each a hand-computed oracle value above).
        self.assertAlmostEqual(plan.total_withdrawn, self.GROSS_ORACLE, places=2)
        engine_tax = plan.total_withdrawn - plan.net_delivered
        self.assertAlmostEqual(engine_tax, self.PROGRESSIVE_TAX, places=2)

        # Closed-form path vs. an independent tax_on_income-delta recomputation
        # over the slice the engine actually drew — they must agree exactly
        # (no residual flat-rate leakage in the bracket walk).
        recomputed_tax = (
            tax_on_income(self.OTHER_TAXABLE + plan.taxable_withdrawn, BRACKETS)
            - tax_on_income(self.OTHER_TAXABLE, BRACKETS))
        self.assertAlmostEqual(engine_tax, recomputed_tax, places=6)
        # Money conservation: it delivered exactly the requested net.
        self.assertAlmostEqual(plan.net_delivered, net_need, places=6)


class DrawdownOASClawbackOracle(unittest.TestCase):
    """Fixture 2 — a single retiree squarely in the OAS-clawback range.

    Fabricated (DP#15/#4): same $25,000 of other taxable income (CPP $15,000 +
    pension $10,000) plus full OAS. A $50,000 net drawdown target grosses up
    (at the flat rate) to a ~$78,272 RRSP draw, taking net income for the
    recovery-tax calc to ~$112,180 — above the $95,323 threshold and below the
    ~$154,710 full-clawback point, i.e. squarely in the PARTIAL-clawback zone
    where the 15 % recovery tax is a live marginal cost.
    """

    CPP = 15_000.0
    PENSION = 10_000.0
    OTHER_TAXABLE = CPP + PENSION
    NET_NEED = 50_000.0

    OAS_MAX = get_oas_annual_max(YEAR)                 # full annual OAS
    THRESHOLD = get_oas_clawback_threshold(YEAR)       # recovery-tax threshold

    def test_fixture_is_squarely_in_the_partial_clawback_zone(self):
        """Guard the premise: engine income lands in the partial-clawback band.

        Net income for the recovery tax (CRA line 23400 — includes CPP,
        pension, the RRSP draw, AND the OAS itself) must sit strictly between
        the threshold and the full-clawback point, so 15 cents on the marginal
        RRSP dollar is genuinely at stake.
        """
        flat_rate = _flat_engine_rate(self.OTHER_TAXABLE, self.NET_NEED)
        plan = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=flat_rate,
        )
        net_income = self.OTHER_TAXABLE + plan.total_withdrawn + self.OAS_MAX
        full_clawback_point = self.THRESHOLD + self.OAS_MAX / 0.15
        self.assertGreater(net_income, self.THRESHOLD)
        self.assertLess(net_income, full_clawback_point)

    def test_current_engine_rate_ignores_the_15pct_clawback(self):
        """TODAY'S BEHAVIOUR (green): the flat rate carries NO clawback term.

        The engine's drawdown rate is a pure tax marginal rate. In the
        clawback zone the true marginal cost of an RRSP dollar is that tax rate
        PLUS 0.15 (OAS recovered at 15 %). Today's engine charges only the tax
        rate, so it under-prices — and therefore under-draws — the RRSP dollar.
        """
        flat_rate = _flat_engine_rate(self.OTHER_TAXABLE, self.NET_NEED)
        plan = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=flat_rate,
        )

        # Pure tax marginal rate (no clawback component) — 0.3612 here.
        tax_marginal = marginal_rate(
            self.OTHER_TAXABLE + plan.total_withdrawn, BRACKETS)
        self.assertEqual(flat_rate, tax_marginal)
        self.assertLess(flat_rate, tax_marginal + 0.15)

        # The recovery tax the drawdown ignored is real and non-trivial — the
        # CRA oracle computes it directly from the clawback primitive.
        net_income = self.OTHER_TAXABLE + plan.total_withdrawn + self.OAS_MAX
        cb = oas_clawback(
            net_income, threshold=self.THRESHOLD, oas_amount=self.OAS_MAX)
        self.assertGreater(cb['clawback_amount'], 0.0)
        self.assertAlmostEqual(cb['clawback_amount'], 2_528.51393, places=4)

        # Engine believes it delivered the full $50,000 net...
        self.assertAlmostEqual(plan.net_delivered, self.NET_NEED, places=6)

    def test_fixture2_clawback_aware_net(self):
        """PR 2 (#363) landed: the OAS clawback is folded into the draw (was
        expectedFailure).

        A clawback-aware drawdown grosses the RRSP draw up to REPLACE the OAS
        the draw claws back, so the household still keeps the full requested net
        after the 15 % recovery tax. PR 2 flipped this to green by passing the
        gross OAS + recovery threshold to ``plan_drawdown_net`` (and the
        year-versioned ``brackets`` so the recovery tax stacks on the real
        progressive rate, not fixture 1's flat placeholder).

        Every expected figure is recomputed here from the pure ``oas_clawback``
        and ``tax_on_income`` primitives on the income the engine's own draw
        produces — never copied from engine output — so the assertions are a
        CRA-style oracle the engine must match to the dollar.
        """
        plan = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.9,                       # bogus flat rate — ignored
            other_taxable_income=self.OTHER_TAXABLE,
            brackets=BRACKETS,
            oas_gross=self.OAS_MAX,
            oas_clawback_threshold=self.THRESHOLD,
        )
        # RRSP is fully taxable, so the taxable draw IS the gross draw, and the
        # CRA net income for the recovery tax is CPP + pension + draw + OAS.
        net_income = self.OTHER_TAXABLE + plan.total_withdrawn + self.OAS_MAX
        cb = oas_clawback(
            net_income, threshold=self.THRESHOLD, oas_amount=self.OAS_MAX)

        # It is genuinely in the PARTIAL-clawback band (not saturated) — 15
        # cents on the marginal draw dollar is really at stake.
        self.assertGreater(cb['clawback_amount'], 0.0)
        self.assertLess(cb['clawback_amount'], self.OAS_MAX)

        # (1) The engine's clawback == the CRA oracle on the resulting income,
        #     to the dollar (this is the fixpoint being *at* its fixed point:
        #     the clawback the draw booked equals the clawback its own income
        #     implies).
        self.assertAlmostEqual(plan.oas_clawback, cb['clawback_amount'], places=2)

        # (2) The draw's after-income-tax proceeds match the progressive
        #     tax_on_income delta over the slice it drew — no flat-rate leakage.
        income_tax = (
            tax_on_income(self.OTHER_TAXABLE + plan.taxable_withdrawn, BRACKETS)
            - tax_on_income(self.OTHER_TAXABLE, BRACKETS))
        self.assertAlmostEqual(
            plan.net_delivered, plan.total_withdrawn - income_tax, places=6)

        # (3) Made whole to the dollar: the after-income-tax proceeds cover the
        #     requested net PLUS the OAS recovered by the draw, so once the
        #     recovery tax is paid the household still nets NET_NEED exactly.
        cra_true_net = plan.net_delivered - plan.oas_clawback
        self.assertAlmostEqual(cra_true_net, self.NET_NEED, places=2)

        # (4) The defect quantified: a clawback-BLIND progressive draw (PR 1)
        #     for the same net would under-draw — the clawback fold makes the
        #     engine draw strictly MORE gross to stay whole.
        blind = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.9,
            other_taxable_income=self.OTHER_TAXABLE,
            brackets=BRACKETS,
        )
        self.assertEqual(blind.oas_clawback, 0.0)
        self.assertGreater(plan.total_withdrawn, blind.total_withdrawn)
        # The extra gross is exactly what the recovery tax + its own tax cost:
        # net_delivered rose by the clawback, grossed up at the marginal rate.
        self.assertAlmostEqual(
            plan.net_delivered - blind.net_delivered, plan.oas_clawback, places=2)


class DrawdownOASClawbackFixpoint(unittest.TestCase):
    """PR 2 (#363): the OAS-clawback draw is a fixpoint, resolved by a bounded,
    deterministic solve. These pin the two properties that make that solve
    trustworthy — it is a PURE function landing on the true fixed point
    (independent of how many iterations reach it), and it is MONOTONE."""

    OTHER_TAXABLE = 25_000.0
    OAS_MAX = get_oas_annual_max(YEAR)
    THRESHOLD = get_oas_clawback_threshold(YEAR)

    def _plan(self, net_need):
        return plan_drawdown_net(
            net_need, ['rrsp'], canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
            other_taxable_income=self.OTHER_TAXABLE, brackets=BRACKETS,
            oas_gross=self.OAS_MAX, oas_clawback_threshold=self.THRESHOLD)

    def test_pure_function_identical_on_repeat(self):
        """DP#26: same inputs → byte-identical outputs, every call."""
        a, b = self._plan(50_000.0), self._plan(50_000.0)
        self.assertEqual(a.total_withdrawn, b.total_withdrawn)
        self.assertEqual(a.net_delivered, b.net_delivered)
        self.assertEqual(a.oas_clawback, b.oas_clawback)

    def test_result_is_the_fixed_point_regardless_of_iteration_count(self):
        """The returned clawback SATISFIES its own fixed-point equation:
        ``C == clawback(other_taxable + oas_gross + taxable_draw(net_need + C))``.
        Being at the fixed point means one more iteration — or a hundred — would
        not move it, so the result is independent of the iteration count once
        converged. Proven two ways: (a) the engine result satisfies the
        equation; (b) an INDEPENDENT hand-rolled iteration, started from zero
        and run to convergence, lands on the same clawback to the dollar.
        """
        net_need = 50_000.0
        engine = self._plan(net_need)

        # (a) the fixed-point equation holds on the engine's own output.
        income = self.OTHER_TAXABLE + self.OAS_MAX + engine.taxable_withdrawn
        C_star = oas_clawback(
            income, threshold=self.THRESHOLD, oas_amount=self.OAS_MAX)['clawback_amount']
        self.assertAlmostEqual(engine.oas_clawback, C_star, places=6)

        # (b) an independent iteration reaches the same fixed point. Each pass
        # draws (clawback-FREE) to net_need + C, then recomputes C on the
        # resulting income. Record the trajectory to show it settles and does
        # not depend on running "a few more" rounds.
        def clawback_free_taxable(net_target):
            p = plan_drawdown_net(
                net_target, ['rrsp'], canada={'rrsp_balance': BIG_RRSP},
                non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
                other_taxable_income=self.OTHER_TAXABLE, brackets=BRACKETS)
            return p.taxable_withdrawn

        def iterate(n_rounds):
            C = 0.0
            for _ in range(n_rounds):
                inc = self.OTHER_TAXABLE + self.OAS_MAX + clawback_free_taxable(net_need + C)
                C = oas_clawback(inc, threshold=self.THRESHOLD,
                                 oas_amount=self.OAS_MAX)['clawback_amount']
            return C

        # Converged by ~15 rounds; 15, 30 and 60 rounds agree, and all match the
        # engine — the hallmark of a fixed point (not an iteration-count artifact).
        self.assertAlmostEqual(iterate(15), engine.oas_clawback, places=2)
        self.assertAlmostEqual(iterate(30), iterate(60), places=6)
        self.assertAlmostEqual(iterate(60), engine.oas_clawback, places=2)

    def test_monotonic_in_net_need(self):
        """A larger net need draws strictly more gross and claws back at least
        as much OAS (the recovery tax is non-decreasing until it saturates)."""
        small, large = self._plan(40_000.0), self._plan(70_000.0)
        self.assertGreater(large.total_withdrawn, small.total_withdrawn)
        self.assertGreaterEqual(large.oas_clawback, small.oas_clawback)
        # Both still in the partial band here, so it strictly increases.
        self.assertGreater(large.oas_clawback, small.oas_clawback)

    def test_non_convergence_is_a_loud_failure_not_a_silent_draw(self):
        """AGENTS.md: prefer the loud failure. If the clawback fixpoint is
        starved of iterations (here forced to a single pass, well short of the
        ~15 a real draw needs), it must RAISE rather than return an
        under-resolved draw. Statutory brackets converge inside the real cap, so
        this path is unreachable in production — the test reaches it by shrinking
        the cap, which also proves the cap is the only thing standing between a
        converged and a non-converged answer (i.e. the result is iteration-count
        independent only BECAUSE it converges)."""
        original = rt._MAX_CLAWBACK_ITERS
        rt._MAX_CLAWBACK_ITERS = 1
        try:
            with self.assertRaises(RuntimeError):
                self._plan(50_000.0)
        finally:
            rt._MAX_CLAWBACK_ITERS = original
        # And with the real cap restored it converges cleanly (no leak).
        self.assertGreater(self._plan(50_000.0).oas_clawback, 0.0)

    def test_clawback_only_raises_the_draw(self):
        """Folding the clawback in never REDUCES the gross vs the clawback-free
        draw — monotone in the clawback itself (0 below threshold, positive in
        the band)."""
        need = 50_000.0
        with_claw = self._plan(need)
        without = plan_drawdown_net(
            need, ['rrsp'], canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
            other_taxable_income=self.OTHER_TAXABLE, brackets=BRACKETS)
        self.assertGreaterEqual(with_claw.total_withdrawn, without.total_withdrawn)
        self.assertGreater(with_claw.oas_clawback, 0.0)


class DrawdownProgressiveRegimes(unittest.TestCase):
    """PR 1 (#363) coverage of the progressive bracket walk across regimes.

    Every expectation is recomputed in-test from the pure ``tax_on_income``
    primitive (never copied from engine output), so a reader sees "this is the
    arithmetic a CRA calculator produces". Each case also feeds a deliberately
    wrong flat ``marginal_rate`` (0.9) to prove the scalar is IGNORED once
    ``brackets`` is supplied.
    """

    def _assert_progressive_rrsp(self, base, gross):
        """A fully-taxable RRSP draw of ``gross`` on top of ``base`` income:
        the engine, asked for the net that draw delivers under progressive tax,
        must reproduce exactly ``gross`` and the tax_on_income-delta tax."""
        tax = tax_on_income(base + gross, BRACKETS) - tax_on_income(base, BRACKETS)
        net = gross - tax
        plan = plan_drawdown_net(
            net, ['rrsp'], canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.9,                 # bogus flat rate — must be ignored
            other_taxable_income=base, brackets=BRACKETS)
        self.assertAlmostEqual(plan.total_withdrawn, gross, places=2)
        self.assertAlmostEqual(plan.total_withdrawn - plan.net_delivered, tax, places=2)
        self.assertAlmostEqual(plan.net_delivered, net, places=6)

    def test_draw_spanning_lower_brackets(self):
        """$25k base + $60k draw — crosses 0.2569 -> 0.3069 -> 0.3612."""
        self._assert_progressive_rrsp(base=25_000.0, gross=60_000.0)

    def test_draw_climbs_into_the_top_bracket(self):
        """Base already in the 0.4996 band; a $60k draw crosses $258,482 into
        the unbounded 0.5331 top bracket (exercises the infinite-ceiling walk)."""
        self._assert_progressive_rrsp(base=250_000.0, gross=60_000.0)

    def test_non_reg_gain_slice_is_re_bracketed(self):
        """Only the accrued-gain fraction of a non-reg draw is taxable, and it
        too is priced progressively against the running income."""
        bal, acb = 400_000.0, 200_000.0
        inclusion = ((bal - acb) / bal) * 0.5      # gain_frac 0.5 x cg_inclusion 0.5
        gross = 80_000.0
        base = 25_000.0
        taxable = gross * inclusion                 # $20k taxable gain
        tax = tax_on_income(base + taxable, BRACKETS) - tax_on_income(base, BRACKETS)
        net = gross - tax
        plan = plan_drawdown_net(
            net, ['non_reg'], canada={},
            non_reg_balance=bal, non_reg_acb=acb,
            marginal_rate=0.9, other_taxable_income=base, brackets=BRACKETS)
        self.assertAlmostEqual(plan.total_withdrawn, gross, places=2)
        self.assertAlmostEqual(plan.taxable_withdrawn, taxable, places=2)
        self.assertAlmostEqual(plan.total_withdrawn - plan.net_delivered, tax, places=2)
        self.assertAlmostEqual(plan.net_delivered, net, places=6)

    def test_running_income_stacks_across_two_taxable_sources(self):
        """RRSP (only $30k) then LIF: the LIF slice re-brackets ON TOP of the
        RRSP draw, so the combined tax equals ONE progressive delta over the
        summed taxable withdrawal."""
        base = 25_000.0
        plan = plan_drawdown_net(
            80_000.0, ['rrsp', 'lif'],
            canada={'rrsp_balance': 30_000.0, 'lif_balance': 500_000.0},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.9, other_taxable_income=base, brackets=BRACKETS)
        self.assertAlmostEqual(plan.net_delivered, 80_000.0, places=4)
        combined_tax = (tax_on_income(base + plan.taxable_withdrawn, BRACKETS)
                        - tax_on_income(base, BRACKETS))
        self.assertAlmostEqual(
            plan.total_withdrawn - plan.net_delivered, combined_tax, places=2)

    def test_balance_limited_draw_delivers_partial_net(self):
        """Only $10k of RRSP against a $50k need — the walk stops at the
        balance and reports the partial net actually deliverable."""
        plan = plan_drawdown_net(
            50_000.0, ['rrsp'], canada={'rrsp_balance': 10_000.0},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.3, other_taxable_income=25_000.0, brackets=BRACKETS)
        self.assertAlmostEqual(plan.total_withdrawn, 10_000.0, places=6)
        self.assertLess(plan.net_delivered, 50_000.0)
        tax = tax_on_income(35_000.0, BRACKETS) - tax_on_income(25_000.0, BRACKETS)
        self.assertAlmostEqual(plan.net_delivered, 10_000.0 - tax, places=4)

    def test_flat_fallback_prices_whole_draw_when_brackets_absent(self):
        """Omitting ``brackets`` keeps the deprecated single-flat-rate path
        (the #579 residual) — one rate for the whole draw."""
        rate, net = 0.3612, 40_000.0
        plan = plan_drawdown_net(
            net, ['rrsp'], canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=rate, other_taxable_income=25_000.0)  # brackets=None
        self.assertAlmostEqual(plan.total_withdrawn, net / (1 - rate), places=6)

    def test_unknown_drawdown_token_is_skipped(self):
        """A token not in the source map is skipped (not an error, not a draw)
        — the draw falls through to the next, real token in the order."""
        plan = plan_drawdown_net(
            10_000.0, ['not_a_real_token', 'rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.3, other_taxable_income=25_000.0, brackets=BRACKETS)
        self.assertGreater(plan.total_withdrawn, 0.0)
        self.assertAlmostEqual(plan.net_delivered, 10_000.0, places=4)

    def test_price_source_draw_edge_guards(self):
        """The pure per-source primitive: non-positive net or gross is a hard
        no-op; a tax-free slice delivers $1 net per $1 gross; the top bracket
        reports an infinite ceiling."""
        from countries.canada.retirement_transition import (
            _price_source_draw, _bracket_at,
        )
        self.assertEqual(
            _price_source_draw(0.0, 1.0, 0.0, 100.0, BRACKETS, 0.3), (0.0, 0.0))
        self.assertEqual(
            _price_source_draw(0.0, 1.0, 100.0, 0.0, BRACKETS, 0.3), (0.0, 0.0))
        self.assertEqual(
            _price_source_draw(0.0, 0.0, 50.0, 100.0, BRACKETS, 0.3), (50.0, 50.0))
        rate, ceil = _bracket_at(300_000.0, BRACKETS)
        self.assertEqual(rate, 0.5331)
        self.assertEqual(ceil, float('inf'))


class DrawdownTwoSpouseSplit(unittest.TestCase):
    """Fixture 3 (#363 PR 4) — the draw split across two spouses' bracket sets.

    Canada has no joint filing: each spouse's RRSP/RRIF withdrawal is taxed on
    THAT spouse's own return, stacked on THAT spouse's own other income. The
    pre-PR-4 engine summed both spouses' income to one household base and priced
    the whole draw against one bracket set (clause (c) of the retired caveat).

    Fabricated (DP#15/#4): a HIGH-income spouse (``primary``, $60,000 of other
    taxable income) and a LOW-income spouse (``spouse``, $15,000). The primary's
    own RRSP is deliberately balance-capped so the draw MUST spill onto the
    spouse's RRSP — exercising both bracket sets. No OAS here, so this fixture
    isolates the bracket split from the clawback (that is the next class).

    Every expected figure is recomputed from the pure ``tax_on_income``
    primitive on the slice each spouse actually draws — never copied from engine
    output — so the assertions are a CRA-style oracle the engine must match.
    """

    OTHER_P = 60_000.0        # high-income spouse's CPP + pension
    OTHER_S = 15_000.0        # low-income spouse's CPP + pension
    GROSS_P = 25_000.0        # primary's RRSP balance (drawn in full — capped)
    GROSS_S = 35_000.0        # the round gross the spouse then draws

    # Per-spouse progressive tax, each on its OWN base (the hand oracle).
    TAX_P = tax_on_income(OTHER_P + GROSS_P, BRACKETS) - tax_on_income(OTHER_P, BRACKETS)
    TAX_S = tax_on_income(OTHER_S + GROSS_S, BRACKETS) - tax_on_income(OTHER_S, BRACKETS)
    NET_P = GROSS_P - TAX_P
    NET_S = GROSS_S - TAX_S
    NET_NEED = NET_P + NET_S

    def _per_member(self):
        return {
            'primary': {'other_taxable_income': self.OTHER_P, 'oas_gross': 0.0,
                        'bracket_target': None, 'bracket_fill_base': None},
            'spouse': {'other_taxable_income': self.OTHER_S, 'oas_gross': 0.0,
                       'bracket_target': None, 'bracket_fill_base': None},
        }

    def test_household_tax_is_the_sum_of_each_spouses_own_progressive_tax(self):
        """The split draw reproduces each spouse's own gross and prices it on
        that spouse's own bracket set, so household tax == TAX_P + TAX_S."""
        plan = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': self.GROSS_P,          # primary — capped
                    'spouse_rrsp_balance': BIG_RRSP},      # spouse — deep
            non_reg_balance=0.0, non_reg_acb=0.0,
            marginal_rate=0.9,                              # ignored (brackets present)
            brackets=BRACKETS,
            per_member=self._per_member())

        # Each spouse drew exactly their oracle gross, against their own stack.
        self.assertAlmostEqual(plan.taxable_by_owner['primary'], self.GROSS_P, places=2)
        self.assertAlmostEqual(plan.taxable_by_owner['spouse'], self.GROSS_S, places=2)
        self.assertAlmostEqual(
            plan.total_withdrawn, self.GROSS_P + self.GROSS_S, places=2)

        # Household tax is the SUM of the two independent progressive taxes.
        household_tax = plan.total_withdrawn - plan.net_delivered
        self.assertAlmostEqual(household_tax, self.TAX_P + self.TAX_S, places=2)
        # Money conservation: it delivered exactly the requested pooled net.
        self.assertAlmostEqual(plan.net_delivered, self.NET_NEED, places=6)

    def test_split_draw_is_strictly_cheaper_than_one_combined_bracket_set(self):
        """Income-splitting can only help or tie: pricing each spouse's slice on
        their own (lower) base is <= stacking the whole draw on the combined
        base. Here it is STRICTLY cheaper — the single-schedule engine grosses
        up MORE to deliver the identical pooled net."""
        split = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': self.GROSS_P, 'spouse_rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
            brackets=BRACKETS, per_member=self._per_member())

        # The pre-PR-4 single-household schedule: ONE combined base, ONE stack,
        # deep pooled RRSP so it is never balance-limited.
        single = plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
            brackets=BRACKETS,
            other_taxable_income=self.OTHER_P + self.OTHER_S)

        # Both deliver the same net; the split draws strictly LESS gross (pays
        # strictly less tax) to do it.
        self.assertAlmostEqual(split.net_delivered, self.NET_NEED, places=6)
        self.assertAlmostEqual(single.net_delivered, self.NET_NEED, places=6)
        split_tax = split.total_withdrawn - split.net_delivered
        single_tax = single.total_withdrawn - single.net_delivered
        self.assertLess(split_tax, single_tax)
        self.assertLess(split.total_withdrawn, single.total_withdrawn)
        # The single schedule is the oracle-provable upper bound: its tax equals
        # one progressive delta over the COMBINED base for the WHOLE draw it drew.
        combined = self.OTHER_P + self.OTHER_S
        recomputed_single = (
            tax_on_income(combined + single.taxable_withdrawn, BRACKETS)
            - tax_on_income(combined, BRACKETS))
        self.assertAlmostEqual(single_tax, recomputed_single, places=2)


class DrawdownTwoSpouseClawback(unittest.TestCase):
    """Fixture 4 (#363 PR 4) — OAS clawback is a PER-INDIVIDUAL calculation.

    The recovery tax is assessed on each person's own net income against their
    own OAS. A draw that pushes the HIGH-income spouse into the clawback band
    must claw back only THAT spouse's OAS; the low-income spouse, still below
    the threshold, keeps all of theirs. The pre-PR-4 engine tested combined
    income against one threshold — over-clawing relative to two per-person ones.
    """

    OTHER_HI = 80_000.0       # high spouse — a modest draw crosses the threshold
    OTHER_LO = 10_000.0       # low spouse — stays well below it even after drawing
    OAS_MAX = get_oas_annual_max(YEAR)
    THRESHOLD = get_oas_clawback_threshold(YEAR)
    NET_NEED = 30_000.0

    def _plan(self):
        # Primary is the HIGH spouse (its own RRSP funds the whole need, so only
        # the primary's income moves — the clean per-person probe).
        return plan_drawdown_net(
            self.NET_NEED, ['rrsp'],
            canada={'rrsp_balance': BIG_RRSP, 'spouse_rrsp_balance': BIG_RRSP},
            non_reg_balance=0.0, non_reg_acb=0.0, marginal_rate=0.9,
            brackets=BRACKETS,
            oas_clawback_threshold=self.THRESHOLD,
            per_member={
                'primary': {'other_taxable_income': self.OTHER_HI,
                            'oas_gross': self.OAS_MAX,
                            'bracket_target': None, 'bracket_fill_base': None},
                'spouse': {'other_taxable_income': self.OTHER_LO,
                           'oas_gross': self.OAS_MAX,
                           'bracket_target': None, 'bracket_fill_base': None},
            })

    def test_only_the_high_spouses_oas_is_clawed_back(self):
        """The booked clawback equals the CRA recovery tax on the HIGH spouse's
        own income alone; the LOW spouse (below threshold) contributes zero."""
        plan = self._plan()
        # The whole taxable draw sat on the primary (its RRSP is drawn first and
        # is deep), so the primary's income carries it and the spouse's does not.
        self.assertGreater(plan.taxable_by_owner['primary'], 0.0)
        self.assertAlmostEqual(plan.taxable_by_owner['spouse'], 0.0, places=6)

        hi_income = self.OTHER_HI + self.OAS_MAX + plan.taxable_by_owner['primary']
        hi_cb = oas_clawback(
            hi_income, threshold=self.THRESHOLD, oas_amount=self.OAS_MAX)
        lo_income = self.OTHER_LO + self.OAS_MAX + plan.taxable_by_owner['spouse']
        lo_cb = oas_clawback(
            lo_income, threshold=self.THRESHOLD, oas_amount=self.OAS_MAX)

        # Only the high spouse is in the band; the low spouse keeps all their OAS.
        self.assertGreater(hi_cb['clawback_amount'], 0.0)
        self.assertEqual(lo_cb['clawback_amount'], 0.0)
        # The engine booked exactly the high spouse's clawback — per person, not
        # combined income against one threshold.
        self.assertAlmostEqual(
            plan.oas_clawback, hi_cb['clawback_amount'], places=2)

    def test_still_whole_after_the_per_person_recovery_tax(self):
        """Made whole to the dollar: after the high spouse's recovery tax is
        paid, the household still nets NET_NEED (the draw grossed up to replace
        exactly the OAS it clawed)."""
        plan = self._plan()
        cra_true_net = plan.net_delivered - plan.oas_clawback
        self.assertAlmostEqual(cra_true_net, self.NET_NEED, places=2)


if __name__ == '__main__':
    unittest.main()
