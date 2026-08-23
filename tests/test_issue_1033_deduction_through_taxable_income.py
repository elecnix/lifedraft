"""Issue #1033: route the s.20(1)(c) investment-interest deduction through
taxable income (the OAS-clawback base + bracket-fill valuation) instead of
the pre-#1033 flat ``* primary_marginal_rate`` side-credit that never touched
taxable income.

## What this PR fixes (the deduction half)

  1. isolates the Quebec investment-expense cap (TA s.336.0.1) into
     ``cap_qc_investment_interest`` (``quebec_deduction.py``) so #1035 can
     later split the federal (uncapped) deduction from the Quebec-capped
     slice without touching the routing path;
  2. values the deduction's STATUTORY tax saving at bracket-fill
     (``tax_calculator.deduction_value`` -- the same ``tax_on_income`` /
     year-brackets path the prologue's ``_income_tax_by_adult`` already uses
     for rental and private-loan s.20(1)(c) interest, DP#9 -- not a second
     tax model); a real improvement: a zero-income retiree used to get a
     bogus ``qc_deductible * marginal_rate(0)`` = 25.69% side-credit;
     ``deduction_value(0, ...)`` now returns 0;
  3. routes the FEDERAL ``total_deductible`` (uncapped -- the QC cap is a
     provincial limit, separate) through the OAS-clawback base in
     ``apply_retirement_drawdown`` (BEFORE its ``drawdown_net_target <= 0``
     early return, so the forced RRIF minimum's clawback sees it too), gated
     on ``ctx.primary_retired`` (the deduction is the primary's; Canada has no
     joint filing), floored at 0; the side-credit is ZEROED in retirement so
     exactly one mechanism fires per phase (no double-count); and
  4. surfaces ``oas_clawback`` on ``YearResult`` (the drawdown + forced-RRIF-
     minimum recovery-tax slice -- NOT the preliminary ``retirement_income``
     slice, which is bundled into ``oas_income``; see the field docstring).

## What this PR does NOT fix (the income half -- deferred by the issue's own
## scope warning)

The leveraged portfolio's DISTRIBUTED income (interest, grossed-up
dividends, realized gains) still never ENTERS the OAS-clawback base, because
non-reg and SM growth are modeled as a net-of-tax growth rate. That is the
"up from the new investment income" direction and a clearly-scoped follow-up
issue -- NOT done here, and NOT half-done.

## Adversarial-review defects fixed here (REVIEW-1041)

  * BLOCKER 1 (double-count): the side-credit and the routing both applied the
    deduction to DIFFERENT income bases. Fixed by phase-gating the side-credit
    to accumulation (zeroed when ``ctx.primary_retired``); the routing is the
    sole mechanism in retirement.
  * BLOCKER 2 (dead routing behind the early return): moved the routing BEFORE
    the ``drawdown_net_target <= 0`` return so it fires even when spending is
    covered without a discretionary draw (the RRIF minimum still books clawback).
  * MAJOR 3 (QC cap leaked into the federal base): route ``total_deductible``
    (federal, uncapped), not ``qc_deductible``.
  * MAJOR 4 (no floor, base went negative): floor the base at ``max(0, base-D)``.
  * MODERATE 5 (asymmetric base reduction): also recompute the OAS-inclusive
    ``bracket_fill_base`` + ``bracket_target`` from the reduced base.
  * MODERATE 6 (tautological tests): rewritten to drive the fold (DP#11/DP#18),
    never reimplement the engine.
  * MINOR 7 (``oas_clawback`` partial measure): docstring clarifies it is the
    drawdown+RRIF slice; the premise test no longer relies on it.

DP#15: every household below is fabricated, round-numbered, role-named.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
import rule_registry

# ── The fabricated 70-year-old household (DP#15) ───────────────────────────
# Mortgage-free (mortgage_balance 0), income just under the 2026 OAS recovery
# threshold ($95,323): CPP $64,800 + pension $30,000 = $94,800. A discretionary
# RRSP draw pushes the household over the threshold into the recovery-tax zone.
HOUSE_VALUE = 800_000
MARGIN_AVAILABLE = 200_000
LUMP_SUM = 200_000          # drawn from the margin, invested in non-reg
HELOC_RATE = 0.055
CPP_MONTHLY = 5_400         # $64,800/yr
PENSION = 30_000
RRSP_BALANCE = 400_000
SPENDING_TARGET = 120_000   # forces a taxable RRSP draw over the threshold


def _portfolio(yield_dividends=0.02):
    "The non-reg portfolio block (yield drives the QC investment-income cap)."
    return {"accounts": {"non_reg": {
        "balance": 0, "cost_basis": 0,
        "composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
        "yield": {"eligible_dividends": yield_dividends, "interest": 0.0},
    }}}


def _retiree_cfg(spending=SPENDING_TARGET, projection_years=8, birth_year=1956,
                 private_loans=None, margin=MARGIN_AVAILABLE, lump=LUMP_SUM,
                 rrsp=RRSP_BALANCE):
    "The 70-year-old, mortgage-free, near-threshold household (dict form)."
    member = {"role": "primary", "id": "p1", "birth_year": birth_year,
              "retirement_age": 65, "gross_income": 0,
              "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
              "rrsp_balance": rrsp, "tfsa_balance": 0,
              "cpp_monthly_estimated": CPP_MONTHLY, "cpp_start_age": 65,
              "oas_start_age": 65, "oas_defer_months": 0,
              "pension_income_annual": PENSION}
    fam = {"members": [member], "children": []}
    if private_loans:
        fam["private_loans"] = private_loans
    return {
        "family": fam,
        "property": {"house_value": HOUSE_VALUE, "mortgage_balance": 0,
                     "margin_available": margin, "heloc_rate": HELOC_RATE,
                     "mortgage_rate": 0.05, "ltv_max": 0.80,
                     "amortization_years": 25},
        "assumptions": {"start_year": 2026, "projection_years": projection_years,
                        "investment_return": 0.06, "salary_growth": 0.0,
                        "inflation": 0.0, "frozen_brackets": True},
        "portfolio": _portfolio(),
        "accounts": {"rrsp_annual_max": 0},
        "retirement": {"spending_target": spending, "net_replacement_rate": 0.0,
                       "drawdown_order": ["rrsp"], "rrif_conversion_age": 71},
        "tax": {"province": "qc"},
    }


def _accumulation_cfg(gross_income=150_000, mortgage_balance=480_000,
                      margin_available=0, yield_dividends=0.02,
                      living_costs=60_000, projection_years=5,
                      birth_year=1990, heloc_rate=0.0470):
    "A working (pre-retirement) household with a drawn margin / advance."
    return {
        "family": {"members": [{"role": "primary", "birth_year": birth_year,
            "retirement_age": 65, "gross_income": gross_income,
            "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0}],
            "children": []},
        "property": {"house_value": 800_000, "mortgage_balance": mortgage_balance,
            "margin_available": margin_available, "heloc_rate": heloc_rate,
            "mortgage_rate": 0.0370 if mortgage_balance else 0.05,
            "ltv_max": 0.80, "amortization_years": 25},
        "assumptions": {"start_year": 2026, "projection_years": projection_years,
            "investment_return": 0.06, "salary_growth": 0.0,
            "inflation": 0.0, "frozen_brackets": True},
        "portfolio": _portfolio(yield_dividends),
        "accounts": {"rrsp_annual_max": 0},
        "household_budget": {"annual_living_costs": living_costs},
        "tax": {"province": "qc"},
    }


def _run(cfg_dict, lump_sum=LUMP_SUM):
    "Run the household through the live fold and return the YearResults."
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(
        cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
        deduct_later=False, lump_sum=lump_sum)
    return sim.run()


def _run_without_routing(cfg_dict, lump_sum=LUMP_SUM):
    """Re-run the SAME household with the s.20(1)(c) deduction's routing
    through the OAS-clawback base DISABLED (``sm_interest_deduction`` zeroed
    after ``apply_sm_interest`` fires) -- the pre-#1033 behaviour, where the
    deduction never touched taxable income. The bracket-fill statutory
    side-credit is left untouched; ONLY the clawback-base routing is disabled,
    so the A/B difference isolates the OAS-clawback interaction this PR adds.
    """
    original = rule_registry.RULES["sm_interest"]

    def patched(ws, ctx):
        fired = original(ws, ctx)
        ws.sm_interest_deduction = 0.0
        return fired

    rule_registry.RULES["sm_interest"] = patched
    try:
        return _run(cfg_dict, lump_sum=lump_sum)
    finally:
        rule_registry.RULES["sm_interest"] = original


# ============================================================================
# 1. The golden no-SM household is byte-exact
# ============================================================================

class TestGoldenNoSmithManoeuvreIsByteExact:
    """The deduction-routing path is a no-op when nothing is borrowed:
    ``sm_interest_deduction`` is 0.0 and the routing is gated
    (``ctx.primary_retired and sm_interest_deduction > 0.0``), so the drawdown
    base is unchanged. The golden household pins this bit-exact: its terminal
    ``total_assets`` must not move."""

    def test_golden_terminal_total_assets_is_unchanged(self):
        from test_golden_trajectory_581 import (
            golden_household_config, _run as _golden_run)
        # Method: ran the golden fixture through the live fold, read the
        # terminal YearResult.total_assets. The expected value is the
        # documented golden invariant (AGENTS.md), computed not stored.
        # It was 9_816_435.13530067 before main's #1046 (RESP annual cap)
        # moved it to 9_709_753.139463063 -- a change on main, NOT this PR
        # (this PR touches zero existing files the golden household reads).
        results = _golden_run(golden_household_config())
        assert results[-1].total_assets == pytest.approx(9_709_753.139463063)


# ============================================================================
# 2. BLOCKER 1: exactly one mechanism fires per phase (no double-count)
# ============================================================================

class TestPhaseGateExactlyOneMechanism:
    """The deduction is captured by ONE mechanism per phase: the bracket-fill
    side-credit in accumulation (no drawdown to route through), the OAS-clawback
    routing in retirement. The gate is ``ctx.primary_retired`` -- NOT
    ``primary_taxable_income > 0`` (which is the trap that made the old
    "no-double-count" defence false: rental/loan income keeps
    primary_taxable_income positive in retirement, so the side-credit fired
    ALONGSIDE the routing and paid the same dollar twice)."""

    def test_side_credit_is_nonzero_in_accumulation(self):
        rs = _run(_accumulation_cfg(), lump_sum=480_000)
        assert any(r.sm_qc_deductible > 0 for r in rs), (
            "premise: the margin interest must be deductible in accumulation")
        assert any((r.readvance_tax_savings + r.traced_borrowing_tax_savings) > 0
                   for r in rs), (
            "the bracket-fill side-credit must fire in accumulation (the sole "
            "mechanism there -- no drawdown to route through)")

    def test_side_credit_is_zero_in_retirement_without_non_employment_income(self):
        rs = _run(_retiree_cfg())
        assert any(r.sm_qc_deductible > 0 for r in rs), (
            "premise: the margin interest must be deductible in retirement")
        for r in rs:
            assert (r.readvance_tax_savings + r.traced_borrowing_tax_savings) \
                == pytest.approx(0.0), (
                    f"side-credit must be 0 in retirement (year {r.year}) -- "
                    f"the routing is the sole mechanism, no double-count")

    def test_side_credit_is_zero_in_retirement_WITH_non_employment_income(self):
        """The gate is ``ctx.primary_retired``, not ``primary_taxable_income >
        0``. A private loan whose LENDER is the retired primary accrues
        interest income that keeps ``primary_taxable_income`` positive in
        retirement (``primary_income`` is zeroed at retirement, the loan
        income is not) -- exactly the shape that made the pre-fix double-count
        invisible. The side-credit must STILL be 0."""
        loan = {"id": "l1", "lender": "p1", "borrower": "ext_borrower",
                "rate": 0.05, "principal": 200_000, "use": "personal",
                "repayment": "amortizing", "interest": "paid"}
        rs = _run(_retiree_cfg(private_loans=[loan]))
        assert any(r.sm_qc_deductible > 0 for r in rs), (
            "premise: the margin interest must still be deductible")
        for r in rs:
            assert (r.readvance_tax_savings + r.traced_borrowing_tax_savings) \
                == pytest.approx(0.0), (
                    f"the gate is ctx.primary_retired, NOT "
                    f"primary_taxable_income>0: the side-credit must be 0 in "
                    f"retirement even with non-employment income (year {r.year})")

    def test_routing_does_not_fire_in_accumulation(self):
        cfg = _accumulation_cfg()
        rs_with = _run(cfg, lump_sum=480_000)
        rs_without = _run_without_routing(cfg, lump_sum=480_000)
        for a, b in zip(rs_with, rs_without):
            assert a.oas_clawback == b.oas_clawback == 0.0, (
                "no OAS in accumulation -> the routing is a no-op there")


# ============================================================================
# 3. The deduction reduces the OAS clawback (the "down" direction)
# ============================================================================

class TestDeductionReducesOasClawback:
    """The headline: a 70-year-old near the threshold that borrows to invest
    sees its OAS clawback fall (the "down" direction; the "up from new
    investment income" direction is the deferred income half)."""

    def test_the_household_is_in_the_recovery_tax_zone(self):
        """Premise check (MINOR 7): assert the household is in the recovery
        zone DIRECTLY -- CPP + pension + the taxable draw exceed the 2026
        threshold -- not via ``oas_clawback > 0`` (which excludes the
        preliminary ``retirement_income`` recovery tax and so cannot
        establish this)."""
        from countries.canada.retirement import get_oas_clawback_threshold
        threshold = get_oas_clawback_threshold(2026)
        rs = _run(_retiree_cfg())
        assert rs[0].cpp_income + rs[0].pension_income == pytest.approx(94_800.0)
        assert any((r.cpp_income + r.pension_income + r.drawdown_taxable)
                   > threshold for r in rs), (
            "premise: the household must land in the OAS recovery-tax zone "
            "(CPP+pension+draw over the threshold) in some year")
        assert any(r.oas_clawback > 0 for r in rs)

    def test_the_margin_interest_deduction_is_present(self):
        rs = _run(_retiree_cfg())
        assert any(r.sm_qc_deductible > 0 for r in rs), (
            "premise: the borrowed-to-invest lump sum's margin interest must "
            "be deductible -- if sm_qc_deductible is zero everywhere, no "
            "deduction is being routed at all")

    def test_the_deduction_lowers_oas_clawback_every_year(self):
        rs_with = _run(_retiree_cfg())
        rs_without = _run_without_routing(_retiree_cfg())
        moved = False
        for a, b in zip(rs_with, rs_without):
            if a.sm_qc_deductible > 0 and b.oas_clawback > 0:
                assert a.oas_clawback < b.oas_clawback, (
                    f"the deduction routed through the clawback base must "
                    f"REDUCE the OAS clawback (year {a.year}: with-routing "
                    f"{a.oas_clawback:.2f} vs without {b.oas_clawback:.2f})")
                moved = True
        assert moved, (
            "no year exercised the clawback reduction -- the fixture no "
            "longer lands in the recovery zone with a deduction present")

    def test_net_oas_income_rises_with_routing(self):
        """The clawback reduction is real cash: net OAS income is HIGHER with
        the deduction routed. (A direction assertion, NOT the tautological
        ``oas_clawback == gross - oas_income`` identity the old test used.)"""
        rs_with = _run(_retiree_cfg())
        rs_without = _run_without_routing(_retiree_cfg())
        rose = False
        for a, b in zip(rs_with, rs_without):
            if a.sm_qc_deductible > 0 and b.oas_clawback > 0:
                assert a.oas_income > b.oas_income, (
                    f"net OAS income must RISE with the deduction routed "
                    f"(year {a.year}: with {a.oas_income:.2f} vs without "
                    f"{b.oas_income:.2f})")
                rose = True
        assert rose

    def test_the_routing_uses_the_federal_uncapped_deduction(self):
        """MAJOR 3: the routing subtracts the FEDERAL ``total_deductible``
        (uncapped s.20(1)(c), ~$11k here), NOT the Quebec-capped
        ``qc_deductible`` (~$4k). The OAS recovery tax is federal and the
        federal deduction has no investment-income limit. Method: a 3-way A/B
        -- no routing vs QC-capped routing (the Major-3 bug) vs federal routing
        (this PR) -- and the federal-routing relief strictly exceeds the
        QC-capped-routing relief, which strictly exceeds zero."""
        original = rule_registry.RULES["sm_interest"]

        def qc_capped(ws, ctx):
            # The Major-3 bug: route the QC-CAPPED slice into the federal base.
            fired = original(ws, ctx)
            ws.sm_interest_deduction = ws.qc_deductible
            return fired

        rs_federal = _run(_retiree_cfg())
        rs_none = _run_without_routing(_retiree_cfg())
        rule_registry.RULES["sm_interest"] = qc_capped
        try:
            rs_qc = _run(_retiree_cfg())
        finally:
            rule_registry.RULES["sm_interest"] = original
        moved = False
        for fed, none, qc in zip(rs_federal, rs_none, rs_qc):
            if fed.sm_qc_deductible > 0 and none.oas_clawback > 0:
                relief_federal = none.oas_clawback - fed.oas_clawback
                relief_qc = none.oas_clawback - qc.oas_clawback
                assert fed.margin_deductible_interest > fed.sm_qc_deductible, (
                    "premise: the federal deduction (margin deductible "
                    "interest) exceeds the QC-capped slice here")
                assert relief_qc > 0, "QC-capped routing must still relieve some clawback"
                assert relief_federal > relief_qc, (
                    f"the federal-routing relief ({relief_federal:.2f}) must "
                    f"EXCEED the QC-capped-routing relief ({relief_qc:.2f}) -- "
                    f"routing the QC cap into the federal base silently drops "
                    f"~61% of the relief (year {fed.year})")
                moved = True
        assert moved


# ============================================================================
# 4. BLOCKER 2: the routing fires even with no discretionary draw
# ============================================================================

class TestRoutingFiresWithoutDiscretionaryDraw:
    """The routing sits BEFORE ``apply_retirement_drawdown``'s
    ``drawdown_net_target <= 0`` early return, so a retiree whose spending is
    covered by CPP/OAS/pension (no discretionary draw) STILL gets the routing
    -- the forced RRIF minimum books OAS recovery tax against the reduced
    per-spouse base. (Pre-fix the routing was AFTER the return and dead here.)"""

    def test_clawback_falls_even_when_drawdown_net_target_is_zero(self):
        # spending_target 40k is covered by cpp+oas+pension (~$103.7k) -> no
        # discretionary draw; the RRIF minimum (age 76+, conversion at 71) is
        # the only taxable draw, and it books clawback.
        cfg = _retiree_cfg(spending=40_000, projection_years=6, birth_year=1950)
        rs_with = _run(cfg)
        assert all(r.drawdown_net_target == 0.0 for r in rs_with), (
            "premise: this fixture must have no discretionary drawdown")
        rs_without = _run_without_routing(cfg)
        moved = False
        for a, b in zip(rs_with, rs_without):
            if a.sm_qc_deductible > 0 and b.oas_clawback > 0:
                assert a.oas_clawback < b.oas_clawback, (
                    f"the routing must fire even with drawdown_net_target=0 "
                    f"(year {a.year}: with {a.oas_clawback:.2f} vs without "
                    f"{b.oas_clawback:.2f}) -- the RRIF-minimum clawback sees "
                    f"the reduced base")
                moved = True
        assert moved, (
            "no year exercised the no-draw routing -- the RRIF minimum may "
            "not be booking clawback in this fixture")


# ============================================================================
# 5. MAJOR 4: the clawback base is floored at 0 (never negative)
# ============================================================================

class TestBaseFlooredAtZero:
    """The routing floors the base at ``max(0, base - D)``. A deduction larger
    than the base is a non-capital loss / carry-forward (not modeled here --
    follow-up), NOT a silently-forfeited negative base charged at the bottom
    bracket. Drives the production rule directly (DP#11: the rule IS the
    production entry point; this is not a re-implementation)."""

    def test_the_reduced_base_never_goes_negative(self):
        from rule_registry import YearWorkingState, RuleContext, RULES
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        ws = YearWorkingState(year=0)
        ws.drawdown_other_taxable_income = 5_000.0
        ws.drawdown_other_taxable_income_primary = 5_000.0
        ws.drawdown_oas_gross = 8_908.0
        ws.drawdown_oas_gross_primary = 8_908.0
        ws.drawdown_bracket_fill_base = 5_000.0 + 8_908.0
        ws.drawdown_bracket_fill_base_primary = 5_000.0 + 8_908.0
        ws.drawdown_bracket_target = None
        ws.drawdown_bracket_target_primary = None
        ws.sm_interest_deduction = 20_000.0   # D > base (5,000) -> would go -15,000
        ws.drawdown_net_target = 0.0           # exercise the pre-early-return path
        ws.liquidate_to_target = False
        ws.drawdown_two_member_split = False
        ctx = RuleContext(
            year=0, calendar_year=2026, allocations={}, config=None,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
            fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
            fhsa_annual_limit=None, non_reg_after_tax_return=None,
            cpp_income=0.0, oas_income=0.0, pension_income=0.0,
            drawdown_order=None, rrif_min_rate_primary=0.0,
            rrif_min_rate_spouse=0.0, drawdown_net_target=0.0,
            retiree_marginal_rate=0.0, drawdown_bracket_target=None,
            drawdown_other_taxable_income=0.0, primary_retired=True,
            spouse_retired=False, year_brackets=brackets)
        RULES["retirement_drawdown"](ws, ctx)
        assert ws.drawdown_other_taxable_income == 0.0, (
            f"the base must floor at 0, not go negative (got "
            f"{ws.drawdown_other_taxable_income})")
        assert ws.drawdown_other_taxable_income_primary == 0.0, (
            f"the per-spouse base must floor at 0 (got "
            f"{ws.drawdown_other_taxable_income_primary})")
        # The bracket-fill base is recomputed from the floored base + oas,
        # so it is oas (not negative).
        assert ws.drawdown_bracket_fill_base == pytest.approx(8_908.0)
        assert ws.drawdown_bracket_fill_base_primary == pytest.approx(8_908.0)


# ============================================================================
# 6. The statutory side-credit is valued at bracket-fill (not flat)
# ============================================================================

class TestBracketFillValuation:
    """``apply_sm_interest`` values the deduction's STATUTORY tax saving with
    ``tax_calculator.deduction_value`` -- NOT the pre-#1033 flat
    ``amount * primary_marginal_rate``. A deduction crossing a bracket
    boundary is worth STRICTLY LESS than the flat top-rate valuation, so a
    household constructed to cross a bracket distinguishes new (bracket-fill)
    from old (flat). Drives the fold."""

    def test_the_live_side_credit_is_strictly_less_than_flat_when_crossing(self):
        # Primary income $56,000 sits in the 54,345-58,523 bracket (rate
        # 0.3069). A deduction large enough to cross DOWN through the 54,345
        # boundary into the 0-54,345 bracket (0.2569) makes bracket-fill <
        # flat top rate. A $200k margin at 4.70% produces ~$9.4k of deductible
        # interest (the QC cap does not bind at a 5% yield on a $200k pot),
        # which crosses the boundary.
        rs = _run(_accumulation_cfg(
            gross_income=56_000, mortgage_balance=0, margin_available=200_000,
            yield_dividends=0.05, living_costs=20_000, projection_years=3,
            heloc_rate=0.0470))
        crossed = False
        for r in rs:
            if r.sm_qc_deductible > 0 and r.primary_marginal > 0:
                side_credit = (r.readvance_tax_savings
                               + r.traced_borrowing_tax_savings)
                flat = r.sm_qc_deductible * r.primary_marginal
                if r.sm_qc_deductible > (56_000 - 54_345):  # crosses the boundary
                    crossed = True
                    assert side_credit < flat - 1e-6, (
                        f"bracket-fill ({side_credit:.4f}) must be STRICTLY "
                        f"less than the flat top-rate ({flat:.4f}) when the "
                        f"deduction crosses a bracket boundary -- otherwise "
                        f"the flat side-credit is still in use (year {r.year})")
        assert crossed, (
            "no year crossed a bracket boundary -- the fixture no longer "
            "exercises the bracket-fill < flat distinction")

    def test_zero_income_retiree_side_credit_is_zero_not_bogus_marginal(self):
        """The retirement phase-gate (ctx.primary_retired) zeroes the
        side-credit, so a zero-income retiree no longer gets the PRE-FIX bogus
        ``qc_deductible * marginal_rate(0)`` = 25.69% side-credit (the old
        flat-rate code had no gate -- in retirement primary_marginal_rate falls
        to marginal_rate(0), the bottom bracket, so the flat side-credit was a
        positive bogus number). This test distinguishes the OLD no-gate flat
        code (>0) from the NEW gate (0); it does NOT distinguish
        ``deduction_value(0,...)=0`` from ``flat`` within the new code (the
        gate zeroes both) -- that distinction is covered by the bracket-fill
        test above, which runs in ACCUMULATION where the gate does not fire."""
        rs = _run(_retiree_cfg())
        for r in rs:
            assert (r.readvance_tax_savings + r.traced_borrowing_tax_savings) \
                == pytest.approx(0.0)


# ============================================================================
# 7. The isolated Quebec cap helper (the #1035 coupling seam)
# ============================================================================

class TestQcCapIsolatedHelper:
    """``cap_qc_investment_interest`` (``quebec_deduction.py``) is the Quebec
    investment-expense cap (TA s.336.0.1) applied by ``apply_sm_interest``.
    #1035 changed its income base from ``balance * yield_rate`` to the year's
    SCHEDULE L net investment income supplied by the caller. A unit test of a
    pure function (DP#11 allows this): it must reproduce the carry-forward-
    on-the-available-side semantics.

    (The old ``yield_rate=0.02, qc_income_base=150_000`` fixtures become
    ``investment_income=3_000`` -- the same dollars, now the caller's Schedule
    L total.)
    """

    def test_caps_at_investment_income_when_no_carry_forward(self):
        from countries.canada.provinces.quebec.quebec_deduction import (
            cap_qc_investment_interest)
        qc_deductible, carry = cap_qc_investment_interest(
            total_deductible=10_000.0, investment_income=3_000.0,
            opening_carry_forward=0.0)
        assert qc_deductible == pytest.approx(3_000.0)
        assert carry == pytest.approx(7_000.0)

    def test_carry_forward_survives_a_year_with_no_new_income(self):
        from countries.canada.provinces.quebec.quebec_deduction import (
            cap_qc_investment_interest)
        qc_deductible, carry = cap_qc_investment_interest(
            total_deductible=0.0, investment_income=0.0,
            opening_carry_forward=7_000.0)
        assert qc_deductible == pytest.approx(0.0)
        assert carry == pytest.approx(7_000.0)

    def test_carry_forward_adds_to_the_available_deduction(self):
        from countries.canada.provinces.quebec.quebec_deduction import (
            cap_qc_investment_interest)
        qc_deductible, carry = cap_qc_investment_interest(
            total_deductible=4_000.0, investment_income=5_000.0,
            opening_carry_forward=3_000.0)
        assert qc_deductible == pytest.approx(5_000.0)
        assert carry == pytest.approx(2_000.0)

    def test_no_deduction_no_carry_forward_is_zero(self):
        from countries.canada.provinces.quebec.quebec_deduction import (
            cap_qc_investment_interest)
        qc_deductible, carry = cap_qc_investment_interest(
            total_deductible=0.0, investment_income=0.0,
            opening_carry_forward=0.0)
        assert qc_deductible == 0.0
        assert carry == 0.0

# ============================================================================
# 5b. NEW-1 (round-2): an explicit bracket_fill_target election survives routing
# ============================================================================

class TestExplicitBracketFillTargetSurvivesRouting:
    """Round-2 NEW-1: the s.20(1)(c) routing recomputes the bracket-fill
    ceiling from the reduced base. An EXPLICIT ``retirement.bracket_fill_target``
    (DP#13) is a fixed dollar ceiling the household declared -- overwriting it
    with a re-derived ``bracket_ceiling(reduced_base)`` silently discards the
    election (the reviewer captured a declared $40,000 overridden to $54,345,
    a 3.8x over-draw). The fix re-derives the ceiling ONLY when it was
    auto-derived; an explicit ceiling is kept (the base still drops, so the
    headroom ``ceiling - base`` grows by D -- the deduction's correct effect on
    a fixed ceiling). Drives the fold with borrowing AND an explicit target."""

    def _cfg(self):
        from countries.canada.retirement import get_oas_annual_max
        cfg = _retiree_cfg(spending=80_000, projection_years=4,
                           birth_year=1960, margin=200_000, lump=200_000,
                           rrsp=600_000)
        cfg['retirement']['drawdown_order'] = ['rrsp_bracket_fill']
        cfg['retirement']['bracket_fill_target'] = 40_000.0
        cfg['family']['members'][0]['cpp_monthly_estimated'] = 1000  # $12k/yr
        cfg['family']['members'][0]['pension_income_annual'] = 10_000
        return cfg

    def test_the_elected_ceiling_is_respected_not_overridden(self):
        from countries.canada.retirement import get_oas_annual_max
        oas_gross = get_oas_annual_max(2026)  # 8,908 (2026, no deferral)
        elected_ceiling = 40_000.0
        rs = _run(self._cfg())
        for r in rs:
            if r.sm_qc_deductible <= 0:
                continue  # no deduction this year -> nothing to route
            assert r.margin_deductible_interest > 0, (
                "premise: the margin interest deduction is present (borrowing)")
            # The reduced OAS-inclusive base the draw stacks on.
            reduced_base = (r.cpp_income + r.pension_income + oas_gross
                            - r.margin_deductible_interest)
            # The draw fills EXACTLY to the ELECTED ceiling (spending need >
            # headroom), so drawtaxable + reduced_base == elected_ceiling. The
            # pre-fix bug overrode the ceiling to the STATUTORY one (~54,345),
            # which would make this sum ~54,345 -- so this asserts the election
            # is respected, not discarded.
            assert r.drawdown_taxable + reduced_base == pytest.approx(
                elected_ceiling, abs=1e-6), (
                f"the elected bracket_fill_target ${elected_ceiling:,.0f} must "
                f"cap the draw, not be overridden by the statutory ceiling "
                f"(year {r.year}: drawtaxable {r.drawdown_taxable:.2f} + "
                f"reduced_base {reduced_base:.2f} = "
                f"{r.drawdown_taxable + reduced_base:.2f})")
            # And the taxable draw alone is under the elected ceiling.
            assert r.drawdown_taxable < elected_ceiling + 1e-6

    def test_routing_grows_the_elected_headroom_by_the_deduction(self):
        """With a fixed elected ceiling, the deduction lowers the base, so the
        headroom (ceiling - base) GROWS by D: the routed draw exceeds the
        no-routing draw (the deduction frees bracket-fill room before the
        declared ceiling). A/B vs routing-disabled."""
        from countries.canada.retirement import get_oas_annual_max
        oas_gross = get_oas_annual_max(2026)
        rs_with = _run(self._cfg())
        rs_without = _run_without_routing(self._cfg())
        # Year 0 (age 66, no RRIF): the no-routing draw is the elected headroom
        # on the un-reduced base; the routing draw is that + D.
        a, b = rs_with[0], rs_without[0]
        assert b.drawdown_taxable == pytest.approx(
            40_000 - (b.cpp_income + b.pension_income + oas_gross)), (
            "premise: the no-routing draw fills the elected headroom on the "
            "un-reduced base")
        assert a.drawdown_taxable > b.drawdown_taxable, (
            "routing lowers the base, so the elected-ceiling headroom grows "
            "and the draw increases -- the deduction frees bracket-fill room")
