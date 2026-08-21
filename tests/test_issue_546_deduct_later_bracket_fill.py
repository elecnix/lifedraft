#!/usr/bin/env python3
"""Issue #546 — Deduct-later valued at bracket-fill rates, not the flat top rate.

A deferred RRSP deduction is worth the marginal rate of the income slice it
removes, not the year's flat top rate. As staggered claims draw income toward
the bracket target, later slices fall through progressively lower brackets and
are worth less.

Round, synthetic figures only (DP#4/DP#15).
"""

from tax_data import default_tax_provider
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_config import SimulationConfig
from simulation_state import RRSPListLedger, SimState, simulate_year_pure
from tax_calculator import (
    deduction_value,
    marginal_rate,
    tax_on_income,
)

BRACKETS = default_tax_provider().get_combined_brackets(2026, province="quebec")


def test_deduction_value_is_tax_difference():
    income, amount = 300000, 80000
    expected = tax_on_income(income, BRACKETS) - tax_on_income(income - amount, BRACKETS)
    assert deduction_value(income, amount, BRACKETS) == expected


def test_deduction_value_below_top_rate_when_crossing_brackets():
    income, amount = 300000, 200000
    flat_top = amount * marginal_rate(income, BRACKETS)
    assert deduction_value(income, amount, BRACKETS) < flat_top


def test_deduction_value_zero_for_nonpositive_amount():
    assert deduction_value(300000, 0, BRACKETS) == 0.0
    assert deduction_value(300000, -50, BRACKETS) == 0.0


def _staggered_sum(income, slices):
    """Reference: value each slice at the bracket-fill rate as income drops."""
    total = 0.0
    running = income
    for amt in slices:
        total += deduction_value(running, amt, BRACKETS)
        running -= amt
    return total


def test_ledger_staggered_claim_equals_bracket_fill_sum():
    # A large lump contributed in year 0, then deducted over later years down to
    # a bracket target, with income falling each year toward that target.
    bracket_target = 132245  # a real bracket boundary; keep income above it
    ledger = RRSPListLedger()
    ledger.add_contribution(year=0, amount=150000, role="primary")

    income_by_year = [300000, 250000, 200000]
    realized = 0.0
    expected = 0.0
    for year, income in enumerate(income_by_year, start=1):
        before = income
        claim = ledger.claim_deferred_deduction(
            year=year, income=income, brackets=BRACKETS,
            bracket_target=bracket_target,
        )
        realized += claim["savings"]
        # The claim should equal the bracket-fill sum of its own slices.
        slice_amounts = [c["amount"] for c in claim["claims"]]
        assert abs(claim["savings"] - _staggered_sum(before, slice_amounts)) < 1e-6
        expected += _staggered_sum(before, slice_amounts)

    assert abs(realized - expected) < 1e-6
    # The lump is fully consumed (each year's room exceeds remaining undeducted).
    assert ledger.undeducted_total() == 0.0


def test_deferred_claim_cheaper_than_flat_top_rate():
    # Deferring and claiming progressively is worth less than naively valuing the
    # whole lump at the contribution-year top rate — exactly what #546 fixes.
    income = 300000
    ledger = RRSPListLedger()
    ledger.add_contribution(year=0, amount=120000, role="primary")
    claim = ledger.claim_deferred_deduction(
        year=1, income=income, brackets=BRACKETS, bracket_target=132245,
    )
    flat_top = claim["amount"] * marginal_rate(income, BRACKETS)
    assert claim["savings"] < flat_top


# ── Advantage metric (DP#17): staggered schedule vs deducting the whole lump
# in one year. Defined as staggered_bracket_fill_total - lump_now_total, where
# lump_now_total = deduction_value(first_claim_year_income, total_deducted).
# >= 0 always; > 0 exactly when spreading keeps slices out of low brackets. ──

def _run_pure_years(lump, incomes, bracket_target):
    """Drive simulate_year_pure over `incomes`, deferring a pre-existing lump.

    Returns the final YearResult (its deduction_advantage_vs_now is cumulative).
    """
    cfg = SimulationConfig(
        start_year=2026, province="quebec",
        deduct_later_bracket_target=bracket_target,
    )
    state = SimState.initial(cfg)
    state.jurisdiction_state["canada"]["rrsp_ledger"] = [{
        "year": 2026, "amount": lump, "role": "primary",
        "deducted": False, "deduction_year": None, "deduction_marginal_rate": None,
    }]
    mtr = marginal_rate(incomes[0], BRACKETS)
    res = None
    for y, income in enumerate(incomes):
        res, state = simulate_year_pure(
            state=state, year=y,
            allocations={"_primary_income": income, "_spouse_income": 0},
            config=cfg, investment_return=0.0, deduct_later=True,
            primary_marginal_rate=mtr, spouse_marginal_rate=0.0,
        )
    return res


def _expected_advantage(lump, incomes, bracket_target):
    """Reference: staggered bracket-fill total minus lump-in-first-year value."""
    remaining = lump
    staggered = 0.0
    first_income = None
    total_deducted = 0.0
    for income in incomes:
        room = max(0.0, income - bracket_target)
        amt = min(remaining, room)
        if amt <= 0:
            continue
        if first_income is None:
            first_income = income
        staggered += deduction_value(income, amt, BRACKETS)
        remaining -= amt
        total_deducted += amt
    if total_deducted <= 0:
        return 0.0
    return staggered - deduction_value(first_income, total_deducted, BRACKETS)


def test_advantage_positive_when_staggering_avoids_low_brackets():
    # A 200k lump but only 67,755 of room per year (income 200k, target 132,245),
    # so the deduction must spread over 3 years. Each year's slice stays in the
    # ~47.46% band. Deducting all 200k in one year instead sinks dollars down
    # through the 25.69%/30.69%/... bands — staggering strictly wins.
    bracket_target = 132245
    incomes = [200000, 200000, 200000]
    res = _run_pure_years(200000, incomes, bracket_target)
    expected = _expected_advantage(200000, incomes, bracket_target)
    assert expected > 0  # the scenario genuinely rewards staggering
    assert abs(res.deduction_advantage_vs_now - expected) < 1e-6
    assert res.deduction_advantage_vs_now > 0


def test_advantage_zero_when_lump_fits_one_year_flat_income():
    # Income flat, and the lump fits entirely within a single year's room, so no
    # staggering happens: the staggered schedule IS the lump-now schedule. The
    # advantage must be ~0 (never spuriously negative).
    bracket_target = 132245
    incomes = [300000, 300000, 300000]
    res = _run_pure_years(120000, incomes, bracket_target)  # room 167,755 > 120k
    assert abs(res.deduction_advantage_vs_now) < 1e-6
