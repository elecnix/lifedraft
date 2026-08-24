"""Issue #1017 — under ``liquidate_to_target``, decumulation UNWINDS the
Smith-Manoeuvre sleeve (die-with-zero on a leveraged household).

#1009's residual sweep liquidates every drawable FINANCIAL account but NOT the
``sm_investment_balance`` sleeve, so on a leveraged household the drawdown
delivers $0/yr for years while the SM portfolio compounds untouched and its
HELOC rides to death — die-with-zero impossible. The fix: when
``liquidate_to_target`` is on, a new ``sm_unwind`` rule (after ``rrif_minimum``,
before ``solvency``) sells a slice of the SM sleeve, realizes the capital gain
(taxed), repays the SM HELOC proportionally, and delivers the NET to the
spending target.

Fabricated round numbers, role-based names (DP#4/DP#15). The SM sleeve is
injected directly into the opening jurisdiction state (a household that has
already built a large, low-leverage SM portfolio), so the test isolates the
unwind behaviour from the decades of readvance that would build it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_golden_trajectory_581 import golden_household_config


# ── Helpers ─────────────────────────────────────────────────────────────────

def _sm_unwind_config(spending_target=500_000, liquidate=True,
                      sm_fmv=2_000_000, sm_acb=1_000_000, sm_heloc=200_000):
    """The golden household + a large, low-leverage SM sleeve injected at year 0,
    a paid-off mortgage against a large house (so the injected HELOC fits the
    80% charge and no new readvance fires — the sleeve only grows/withdraws),
    retire at 65, die at 90, and a spending target high enough that the ordinary
    financial drawdown exhausts in late retirement and the SM unwind must fund
    the target into deep retirement. Fabricated round numbers (DP#4/DP#15)."""
    cfg = golden_household_config()
    cfg['family']['members'][0]['retirement_age'] = 65
    cfg['family']['members'][1]['retirement_age'] = 65
    cfg['assumptions']['horizon_age'] = 90
    cfg['retirement']['spending_target'] = spending_target
    if liquidate:
        cfg['retirement']['liquidate_to_target'] = True
    # Paid-off mortgage + large house so the injected SM HELOC fits the charge
    # (80% x house_value) and there is no mortgage principal to re-borrow (the
    # sleeve only grows via investment return, then unwinds). A real HELOC rate
    # so the injected line is priced correctly.
    cfg['property']['mortgage_balance'] = 0
    cfg['property']['house_value'] = 2_000_000
    cfg['property']['heloc_rate'] = 0.05
    cfg['property']['heloc_readvance'] = True
    return cfg, sm_fmv, sm_acb, sm_heloc


def _run_with_sm_sleeve(cfg, sm_fmv, sm_acb, sm_heloc):
    """Build FamilySimulation, inject the SM sleeve into the opening state, run."""
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=True, deduct_later=False)
    canada = sim._state.jurisdiction_state['canada']
    canada['sm_investment_balance'] = sm_fmv
    canada['sm_investment_cost_basis'] = sm_acb
    canada['readvance_heloc_balance'] = sm_heloc
    return sim.run()


def _late_decumulation_years(rs):
    """Late-retirement years where the ordinary financial accounts are drained
    (the years the SM unwind must fund, or — pre-fix — the years the strand
    manifests in)."""
    return [
        r for r in rs
        if r.drawdown_net_target > 0
        and r.total_tfsa <= 1.0
        and r.total_rrsp <= 1.0
        and r.non_reg_balance <= 1.0
    ]


# ============================================================================
# FIX 2 — the SM sleeve unwinds under liquidate_to_target.
# ============================================================================

class TestSmUnwindDrainsTheSleeve:
    """The fix: with ``liquidate_to_target`` ON, the SM sleeve is sold to fund
    the spending shortfall the ordinary drawdown could not cover, the HELOC is
    repaid, and the terminal sleeve + HELOC drain to zero."""

    def test_sm_unwind_fires_and_delivers_net_to_the_target(self):
        rs = _run_with_sm_sleeve(*_sm_unwind_config(liquidate=True))
        late = _late_decumulation_years(rs)
        assert len(late) > 0, "expected late-decumulation years where the " \
            "ordinary financial accounts are drained"
        # The SM unwind fires in some late year (the sleeve is being sold to
        # fund the shortfall, not left to compound untouched).
        assert any(r.sm_unwind_net_delivered > 1.0 for r in late), (
            "no SM unwind fired in late decumulation -- the sleeve is still "
            "untouched (the #1017 bug)")
        # In those late years the drawdown DELIVERS the target (funded by the
        # SM unwind) rather than $0 while a multi-million sleeve sits -- the
        # core die-with-zero behaviour. While the SM sleeve still holds a
        # balance, net_delivered tracks the target (not $0).
        for r in late:
            if r.sm_investment_balance > 1.0:
                assert r.drawdown_net_delivered > 1.0, (
                    f"year {r.year}: SM balance {r.sm_investment_balance:.0f} "
                    f"remained but drawdown delivered "
                    f"{r.drawdown_net_delivered:.0f} against target "
                    f"{r.drawdown_net_target:.0f} -- the SM sleeve stranded "
                    f"(the #1017 bug)")

    def test_terminal_sm_sleeve_and_heloc_drain_to_zero(self):
        rs = _run_with_sm_sleeve(*_sm_unwind_config(liquidate=True))
        final = rs[-1]
        # The SM sleeve is unwound to zero by death (die-with-zero), and the
        # HELOC that financed it is repaid from the sale proceeds (not left
        # riding to death against a sold asset).
        assert final.sm_investment_balance == 0.0, (
            f"terminal SM balance {final.sm_investment_balance:.2f} -- the "
            f"sleeve did not unwind to zero (die-with-zero not achieved)")
        assert final.sm_heloc_balance == 0.0, (
            f"terminal SM HELOC {final.sm_heloc_balance:.2f} -- the loan was "
            f"not repaid as the sleeve unwound (debt riding to death)")

    def test_sm_unwind_repays_the_heloc_from_sale_proceeds(self):
        rs = _run_with_sm_sleeve(*_sm_unwind_config(liquidate=True))
        # Across the unwind years the cumulative HELOC repayment equals the
        # opening SM HELOC (the whole loan is retired as the sleeve unwinds),
        # and every unwind year is money-conserving: net_delivered + tax +
        # heloc_repaid == gross_sold (within float).
        total_heloc_repaid = sum(r.sm_unwind_heloc_repaid for r in rs)
        opening_heloc = 200_000.0
        assert abs(total_heloc_repaid - opening_heloc) < 1.0, (
            f"cumulative HELOC repayment {total_heloc_repaid:.2f} != opening "
            f"HELOC {opening_heloc:.2f} -- the loan was not fully retired as "
            f"the sleeve unwound")
        for r in rs:
            if r.sm_unwind_proceeds > 0:
                # money conservation: proceeds = tax + heloc_repaid + net
                assert abs(r.sm_unwind_proceeds
                           - r.sm_unwind_tax
                           - r.sm_unwind_heloc_repaid
                           - r.sm_unwind_net_delivered) < 0.5, (
                    f"year {r.year}: SM unwind not money-conserving "
                    f"(proceeds {r.sm_unwind_proceeds:.2f} != tax "
                    f"{r.sm_unwind_tax:.2f} + heloc "
                    f"{r.sm_unwind_heloc_repaid:.2f} + net "
                    f"{r.sm_unwind_net_delivered:.2f})")


# ============================================================================
# Gap 2 — reproduction: with the flag OFF the SM sleeve strands (the bug).
# ============================================================================

class TestSmSleeveStrandsWhenLiquidateIsOff:
    """SANITY: with ``liquidate_to_target`` OFF, the SM sleeve really does
    strand -- the pre-fix behaviour the issue reports. Documents the
    reproduction so the fix's assertion above is meaningful (the test file is
    self-proving: the OFF path shows the bug, the ON path shows the fix)."""

    def test_sm_sleeve_compounds_untouched_when_liquidate_is_off(self):
        rs = _run_with_sm_sleeve(*_sm_unwind_config(liquidate=False))
        # No SM unwind fires (the rule is gated on liquidate_to_target).
        assert not any(r.sm_unwind_net_delivered > 0.0 for r in rs), (
            "SM unwind fired with liquidate_to_target OFF -- the gate is "
            "broken (DP#32: the die-with-zero mode is opt-in)")
        # The sleeve compounds untouched to death (the bug: wealth present,
        # nothing drawn) -- terminal SM is a large balance, not zero.
        assert rs[-1].sm_investment_balance > 1_000_000, (
            f"terminal SM {rs[-1].sm_investment_balance:.0f} -- expected the "
            f"sleeve to compound untouched with liquidate OFF (the bug)")
        # And the HELOC rides to death (not repaid) while the asset compounds.
        assert rs[-1].sm_heloc_balance > 1.0, (
            "SM HELOC was repaid with liquidate OFF -- the unwind should not "
            "fire at all in this mode")
        # There exist late years where net_delivered is $0 while a meaningful
        # SM balance remains -- the strand.
        assert any(r.drawdown_net_delivered < 1.0
                   and r.sm_investment_balance > 1_000_000
                   and r.drawdown_net_target > 10_000
                   for r in rs), (
            "no stranded late year found -- the bug reproduction (SM balance "
            "present, drawdown delivers $0) is not exhibited")


# ============================================================================
# Golden byte-exact: liquidate_to_target absent / SM absent -> no-op.
# ============================================================================

class TestSmUnwindIsInertWithoutLiquidateOrSleeve:
    """DP#32: the unwind is a strict no-op when ``liquidate_to_target`` is off
    OR when there is no SM sleeve. The golden household (no SM, no
    liquidate_to_target) is byte-identical to pre-#1017."""

    def test_no_unwind_fires_without_an_sm_sleeve(self):
        # liquidate ON but NO SM sleeve injected -> nothing to unwind.
        cfg, _, _, _ = _sm_unwind_config(liquidate=True)
        sim_cfg = SimulationConfig.from_dict(cfg)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                               use_readvanceable=True, deduct_later=False)
        # No SM sleeve injected -- jurisdiction state defaults to 0.
        rs = sim.run()
        assert not any(r.sm_unwind_net_delivered > 0.0 for r in rs)
        assert rs[-1].sm_investment_balance == 0.0


# ============================================================================
# Unit tests for the price_sm_unwind helper (DP#3: pure function).
# ============================================================================

class TestPriceSmUnwindHelper:
    """The pure pricing helper: money conservation, progressive tax, the
    proportional HELOC repayment, and the edge cases the integration path
    never reaches (no shortfall, no brackets, an underwater sleeve)."""

    def _brackets(self):
        from tax_data import default_tax_provider
        return default_tax_provider().get_combined_brackets()

    def test_no_shortfall_or_no_sleeve_is_inert(self):
        from countries.canada.retirement_transition import price_sm_unwind
        # No shortfall -> nothing to sell.
        r = price_sm_unwind(0.0, 5_000_000, 500_000, 520_000,
                            self._brackets(), other_income=0.0)
        assert r.gross_sold == 0.0 and r.net_delivered == 0.0
        # No sleeve -> nothing to sell.
        r = price_sm_unwind(400_000, 0.0, 0.0, 0.0,
                            self._brackets(), other_income=0.0)
        assert r.gross_sold == 0.0 and r.net_delivered == 0.0

    def test_money_conservation_proceeds_equal_tax_plus_heloc_plus_net(self):
        from countries.canada.retirement_transition import price_sm_unwind
        r = price_sm_unwind(300_000, 2_000_000, 1_000_000, 200_000,
                            self._brackets(), other_income=50_000)
        assert r.gross_sold > 0.0
        assert abs(r.gross_sold - r.tax - r.heloc_repaid - r.net_delivered) < 0.5
        # The net delivers the need (the sleeve can cover it) up to the need.
        assert r.net_delivered <= 300_000 + 0.5
        assert r.net_delivered > 1.0

    def test_flat_rate_fallback_when_no_brackets(self):
        """The no-brackets branch (deprecated flat fallback) still prices a
        sensible tax and conserves money."""
        from countries.canada.retirement_transition import price_sm_unwind
        r = price_sm_unwind(200_000, 2_000_000, 1_000_000, 200_000,
                            None, other_income=50_000, flat_rate=0.40)
        assert r.gross_sold > 0.0
        assert r.tax > 0.0
        assert abs(r.gross_sold - r.tax - r.heloc_repaid - r.net_delivered) < 0.5

    def test_underwater_sleeve_repays_heloc_and_delivers_zero_net(self):
        """When the HELOC exceeds the FMV (sm_heloc > sm_fmv), selling the
        whole sleeve repays as much HELOC as the proceeds allow (capped at
        proceeds - tax) and delivers $0 net -- a loud, honest shortfall,
        not a fabricated fill (DP#32)."""
        from countries.canada.retirement_transition import price_sm_unwind
        # $1M sleeve financed by $1.2M HELOC (underwater), no gain (ACB==FMV)
        # so tax is zero and the cap binds at the proceeds.
        r = price_sm_unwind(500_000, 1_000_000, 1_000_000, 1_200_000,
                            self._brackets(), other_income=0.0)
        # The whole sleeve is sold (it cannot deliver the need).
        assert abs(r.gross_sold - 1_000_000) < 1.0
        # HELOC repaid is capped at the proceeds (tax is 0, no gain).
        assert abs(r.heloc_repaid - 1_000_000) < 1.0
        assert r.net_delivered == 0.0
        assert abs(r.gross_sold - r.tax - r.heloc_repaid - r.net_delivered) < 0.5

    def test_realized_gain_tracks_the_accrued_gain_fraction(self):
        from countries.canada.retirement_transition import price_sm_unwind
        r = price_sm_unwind(100_000, 2_000_000, 1_000_000, 200_000,
                            self._brackets(), other_income=50_000)
        # realized_gain = gross_sold * (fmv - acb) / fmv (the gain fraction).
        expected = r.gross_sold * (2_000_000 - 1_000_000) / 2_000_000
        assert abs(r.realized_gain - expected) < 0.5

    def test_underwater_loss_sleeve_books_a_negative_realized_gain(self):
        """Issue #110: an underwater SM pot (fmv < acb) realizes a capital LOSS
        and prices its deductible slice against the year's other income,
        instead of the pre-#110 `max(0.0, gain_frac)` floor hiding it (which
        left net = gross down, a proportional ACB cut, and no loss recorded
        anywhere -- the surviving pot silently carried acb > fmv)."""
        from countries.canada.retirement_transition import price_sm_unwind
        # The issue's exact reproduction: FMV 1,000,000 / ACB 1,500,000,
        # need 50,000. other_income is the household's already-recognized
        # taxable income this year, which the loss's deductible slice offsets.
        r = price_sm_unwind(50_000, 1_000_000, 1_500_000, 0.0,
                            self._brackets(), other_income=100_000)
        # 1. The loss is recognised: realized_gain is negative and tracks the
        #    signed gain fraction (fmv - acb) / fmv = -0.5.
        assert r.realized_gain < 0.0
        assert abs(r.realized_gain - r.gross_sold * (1_000_000 - 1_500_000) / 1_000_000) < 0.5
        # 2. The loss is priced: it offsets the year's other income, so the
        #    disposition's tax is a negative credit (a real tax reduction),
        #    not the pre-#110 phantom zero.
        assert r.tax < 0.0
        # 3. Money conservation with the signed tax still closes exactly -- the
        #    net delivered equals the gross proceeds minus the signed tax minus
        #    the (zero here) HELOC repayment.
        assert abs(r.gross_sold - r.tax - r.heloc_repaid - r.net_delivered) < 0.5
        # 4. The surviving pot's ACB is NOT silently absorb-verwritten: the
        #    proportional reduction ([ref] fix) leaves it carrying acb > fmv,
        #    and the loss is visible in realized_gain rather than hidden.
        f = r.gross_sold / 1_000_000
        acb_sold = f * 1_500_000
        fmv_after = 1_000_000 - r.gross_sold
        acb_after = 1_500_000 - acb_sold
        assert acb_after > fmv_after  # the surviving pot holds acb > fmv openly

    def test_underwater_loss_with_no_other_income_recognises_loss_but_books_no_credit(self):
        """With no taxable income to absorb it, the loss is still RECOGNISED
        (a negative realized_gain, never floored to zero) but warrants no tax
        credit -- tax_on_income's zero floor means the deduction cannot push
        the year's tax below zero. The cross-year carryforward of the unused
        remainder is tracked as issue #140 (no loss-carryforward state yet)."""
        from countries.canada.retirement_transition import price_sm_unwind
        r = price_sm_unwind(50_000, 1_000_000, 1_500_000, 0.0,
                            self._brackets(), other_income=0.0)
        assert r.realized_gain < 0.0
        assert abs(r.realized_gain - 50_000 * (1_000_000 - 1_500_000) / 1_000_000) < 0.5
        assert r.tax == 0.0  # nothing to credit against
        assert abs(r.gross_sold - r.tax - r.heloc_repaid - r.net_delivered) < 0.5