#!/usr/bin/env python3
"""Issue #286 -- a deduct-now RRSP contribution is refunded at the tax it
actually saves, not at contribution x top marginal rate.

Before #286 the deduct-now path booked ``(p_rrsp + s_rrsp) * primary_marginal_rate``
(and the spouse's own contribution at the spouse's rate) as the refund, in
three places (the deduction rule, ``YearResult.rrsp_tax_savings`` and the
HELOC-paydown refund). A $200,000 catch-up contribution against $150,000 of
income was refunded ~$94,920 -- more than the entire tax on $150,000.

Now each contributor's deduction is claimed through the ledger, valued
bracket-fill against that contributor's own taxable income, and capped at the
amount that still reduces tax; the excess stays undeducted in the ledger and
is claimed in later years. These tests drive ``simulate_year_pure`` /
``FamilySimulation`` and assert the ENGINE's output (DP#11/#18); expected
values are computed with ``tax_on_income`` / ``deduction_value`` on the SAME
brackets passed to the engine as ``year_brackets``. The few ``RRSPListLedger``
tests are labelled unit tests of the pure ledger methods.

Fabricated round numbers and role names only (DP#4/DP#15).
"""

import dataclasses
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import countries.canada  # noqa: F401  (register the jurisdiction adapter)
from rrsp_ledger import (
    RRSPListLedger,
    current_bracket_floor,
    lowest_taxed_floor,
)
from rules_contributions import (
    summarize_rrsp_deduction_carry_forward,
    worst_rrsp_deduction_carry_forward,
)
from simulation_config import SimulationConfig
from simulation_state import SimState, _build_year_inputs, simulate_year_pure
from tax_calculator import deduction_value, marginal_rate, tax_on_income
from tax_data import default_tax_provider

B = default_tax_provider().get_combined_brackets(2026, province="quebec")
FLOOR = lowest_taxed_floor(B)
TOL = 1e-6


def _config(*, primary_income=150_000, primary_room=200_000,
            spouse_income=None, spouse_room=0, bracket_target=0.0):
    members = [{'role': 'primary', 'birth_year': 1980,
                'gross_income': primary_income,
                'rrsp_room_accumulated': primary_room,
                'tfsa_room_accumulated': 0}]
    if spouse_income is not None:
        members.append({'role': 'spouse', 'birth_year': 1982,
                        'gross_income': spouse_income,
                        'rrsp_room_accumulated': spouse_room,
                        'tfsa_room_accumulated': 0})
    return SimulationConfig(
        projection_years=20, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, start_year=2026, province='quebec',
        investment_return=0.0, salary_growth=0.0,
        family_members=members, children=[],
        deduct_later_bracket_target=bracket_target)


def _step(state, year, cfg, *, own=0.0, spousal=0.0, spouse_own=0.0,
          p_income=150_000, s_income=0, deduct_later=False, heloc_rate=0.0):
    allocations = {
        'primary_rrsp': own, 'spousal_rrsp': spousal, 'spouse_rrsp': spouse_own,
        '_primary_income': p_income, '_spouse_income': s_income,
        '_primary_taxable_income': p_income, '_spouse_taxable_income': s_income,
        '_annual_savings': 0,
    }
    return simulate_year_pure(
        state=state, year=year,
        inputs=_build_year_inputs(
            allocations=allocations, config=cfg, investment_return=0.0,
            heloc_rate=heloc_rate, deduct_later=deduct_later,
            primary_marginal_rate=marginal_rate(p_income, B),
            spouse_marginal_rate=marginal_rate(s_income, B),
            year_brackets=B))


def _ledger(state):
    return RRSPListLedger(state.jurisdiction_state['canada']['rrsp_ledger'])


def _undeducted(state, roles=('primary', 'spousal', 'spouse')):
    return sum(e['amount'] for e in _ledger(state)
               if not e['deducted'] and e['role'] in roles)


# ── INV-1 / INV-2: the core cap, and the remainder carried (not lost) ───────

def test_deduct_now_lump_capped_at_useful_deduction():
    """$200k contributed against $150k of income: year-0 savings is the whole
    tax the deduction can remove; the other $50k is carried in the ledger and
    reduces year-1 tax. deduction_value(150k, 200k) == tax(150k), so the
    year-0 figure alone cannot tell a capped claim from an uncapped one --
    the carried amount, the ledger remainder and year-1 savings are the
    discriminating assertions."""
    cfg = _config()
    r0, s1 = _step(SimState.initial(cfg), 0, cfg, own=200_000)

    expected = tax_on_income(150_000, B) - tax_on_income(FLOOR, B)
    assert abs(r0.rrsp_tax_savings - expected) < TOL
    assert r0.rrsp_tax_savings < 200_000 * marginal_rate(150_000, B)
    assert r0.rrsp_tax_savings <= tax_on_income(150_000, B) + TOL
    assert abs(r0.rrsp_deduction_carried_forward - 50_000) < TOL
    assert abs(_undeducted(s1) - 50_000) < TOL

    r1, s2 = _step(s1, 1, cfg)
    assert abs(r1.rrsp_tax_savings - deduction_value(150_000, 50_000, B)) < TOL
    assert r1.rrsp_tax_savings > 0
    assert r1.rrsp_deduction_carried_forward == 0.0
    assert _undeducted(s2) == 0.0
    # INV-6: the ledger's own per-slice rates reproduce the reported refunds.
    assert abs(_ledger(s2).total_tax_savings()
               - (r0.rrsp_tax_savings + r1.rrsp_tax_savings)) < TOL
    assert abs(_ledger(s2).total_deducted() - 200_000) < TOL


@pytest.mark.parametrize('contribution, carried', [
    (100_000, 0.0),        # below the cap: claimed in full
    (150_000, 0.0),        # exactly the cap
    (150_001, 1.0),        # one dollar over: that dollar is carried
])
def test_cap_threshold_both_sides(contribution, carried):
    cfg = _config()
    r0, s1 = _step(SimState.initial(cfg), 0, cfg, own=contribution)
    claimed = contribution - carried
    assert abs(r0.rrsp_tax_savings - deduction_value(150_000, claimed, B)) < TOL
    assert abs(r0.rrsp_deduction_carried_forward - carried) < TOL
    assert abs(_undeducted(s1) - carried) < TOL


def test_within_one_bracket_is_unchanged():
    """A contribution inside the top bracket is worth exactly its size at the
    marginal rate -- the pre-#286 figure, unchanged where it was right."""
    cfg = _config()
    r0, _ = _step(SimState.initial(cfg), 0, cfg, own=5_000)
    assert abs(r0.rrsp_tax_savings - 5_000 * marginal_rate(150_000, B)) < TOL
    assert r0.rrsp_deduction_carried_forward == 0.0


# ── INV-9: each contributor valued against their OWN income ─────────────────

@pytest.mark.parametrize('deduct_later', [False, True])
def test_spouse_contribution_valued_against_spouse_income(deduct_later):
    """The spouse's own $60k against $40k of spouse income: capped at the tax
    on $40k, $20k carried -- on both paths (deduct_later staggers only the
    primary's claim). Not 60k x the spouse's rate, and nothing from the
    primary's $150k."""
    cfg = _config(spouse_income=40_000, spouse_room=60_000)
    r0, s1 = _step(SimState.initial(cfg), 0, cfg, spouse_own=60_000,
                   s_income=40_000, deduct_later=deduct_later)
    expected = tax_on_income(40_000, B) - tax_on_income(FLOOR, B)
    assert abs(r0.rrsp_tax_savings - expected) < TOL
    assert r0.rrsp_tax_savings != pytest.approx(60_000 * marginal_rate(40_000, B))
    assert abs(r0.rrsp_deduction_carried_forward - 20_000) < TOL
    assert abs(_undeducted(s1, ('spouse',)) - 20_000) < TOL


def test_spousal_rrsp_contribution_valued_against_primary_income():
    """A spousal-RRSP contribution is the CONTRIBUTOR's deduction: valued
    against the primary's income, not the annuitant spouse's."""
    cfg = _config(spouse_income=40_000, spouse_room=0)
    r0, _ = _step(SimState.initial(cfg), 0, cfg, spousal=10_000,
                  s_income=40_000)
    assert abs(r0.rrsp_tax_savings - deduction_value(150_000, 10_000, B)) < TOL


# ── INV-10: deduct-later can never lose to a correctly capped deduct-now ────

def _run_years(cfg, n, *, deduct_later):
    state = SimState.initial(cfg)
    results = []
    for y in range(n):
        r, state = _step(state, y, cfg, own=200_000 if y == 0 else 0.0,
                         deduct_later=deduct_later)
        results.append(r)
    return results, state


def test_deduct_later_total_at_least_deduct_now_when_fully_claimed():
    cfg = _config()
    now, s_now = _run_years(cfg, 13, deduct_later=False)
    later, s_later = _run_years(cfg, 13, deduct_later=True)
    # Both schedules drained the whole lump -- otherwise the comparison
    # below would be meaningless.
    assert _undeducted(s_now) == 0.0
    assert _undeducted(s_later) == 0.0
    total_now = sum(r.rrsp_tax_savings for r in now)
    total_later = sum(r.rrsp_tax_savings for r in later)
    assert total_later >= total_now
    # INV-6 on both: the ledger's per-slice rates reproduce the refunds.
    assert abs(_ledger(s_now).total_tax_savings() - total_now) < TOL
    assert abs(_ledger(s_later).total_tax_savings() - total_later) < TOL
    # The deduct-later stagger is a CHOICE, not a cap: never reported as
    # carried forward.
    assert all(r.rrsp_deduction_carried_forward == 0.0 for r in later)


# ── INV-8: two steps in the same tax year share one cap ─────────────────────

def test_same_year_two_steps_do_not_double_claim():
    """The monthly path runs a year-0 lump step, then the regular year-0
    step. Both claim against the SAME year's income."""
    cfg = _config()
    r_lump, s1 = _step(SimState.initial(cfg), 0, cfg, own=200_000)
    r_reg, s2 = _step(s1, 0, cfg)
    assert r_reg.rrsp_tax_savings == 0.0
    claimed_year0 = sum(e['amount'] for e in _ledger(s2)
                        if e['deducted'] and e['deduction_year'] == 0)
    assert claimed_year0 <= 150_000 - FLOOR + TOL
    assert abs(_undeducted(s2) - 50_000) < TOL


def test_same_year_second_step_claims_only_remaining_headroom():
    cfg = _config()
    _, s1 = _step(SimState.initial(cfg), 0, cfg, own=100_000)
    r_reg, s2 = _step(s1, 0, cfg, own=100_000)
    # 100k already claimed against 150k: only 50k of headroom left, valued
    # from 50k down (the running income after the first claim).
    assert abs(r_reg.rrsp_tax_savings - deduction_value(50_000, 50_000, B)) < TOL
    assert abs(r_reg.rrsp_deduction_carried_forward - 50_000) < TOL


# ── INV-7: FIFO across every entry, amounts conserved (unit test) ───────────

def test_fifo_multi_entry_claim_conserves_amounts():
    """UNIT test of the pure ledger method: three undeducted entries, one
    claim -- every entry is visited oldest-first until the cap is exhausted
    (never only the first match), the partial entry is split, and nothing is
    created or lost."""
    ledger = RRSPListLedger()
    ledger.add_contribution(year=-1, amount=30_000, role='primary')
    ledger.add_contribution(year=0, amount=100_000, role='primary')
    ledger.add_contribution(year=1, amount=50_000, role='spousal')
    claim = ledger.claim_useful_deductions(1, 150_000, B, ('primary', 'spousal'))
    assert abs(claim['amount'] - min(180_000, 150_000 - FLOOR)) < TOL
    assert abs(claim['amount'] - sum(c['amount'] for c in claim['claims'])) < TOL
    assert [c['year'] for c in claim['claims']] == [-1, 0, 1]
    assert abs(claim['claims'][2]['amount'] - 20_000) < TOL
    assert abs(claim['carried_forward'] - 30_000) < TOL
    assert abs(ledger.total_deducted() + ledger.undeducted_total() - 180_000) < TOL
    assert abs(claim['savings'] - deduction_value(150_000, 150_000, B)) < TOL


# ── INV-5: one source for the refund, including the HELOC paydown ───────────

def test_rrsp_refund_equals_rrsp_tax_savings_on_heloc_path():
    """With a drawn HELOC the refund pays the balance down. The paydown is
    the capped refund, the same figure YearResult reports -- not the flat
    200k x MTR product (which would have paid down 94,920)."""
    cfg = _config()
    state = dataclasses.replace(SimState.initial(cfg), heloc_balance=100_000.0)
    r0, _ = _step(state, 0, cfg, own=200_000, heloc_rate=0.0)
    paydown = 100_000.0 - r0.heloc_balance
    assert abs(paydown - min(r0.rrsp_tax_savings, 100_000.0)) < TOL
    assert abs(paydown - (tax_on_income(150_000, B) - tax_on_income(FLOOR, B))) < TOL


# ── INV-12: a missing income is loud, never a $0 deduction ──────────────────

def test_missing_income_fails_loudly():
    cfg = _config()
    state = SimState.initial(cfg)
    state.jurisdiction_state['canada']['rrsp_ledger'] = [{
        'year': -1, 'amount': 30_000, 'role': 'primary', 'deducted': False,
        'deduction_year': None, 'deduction_marginal_rate': None}]
    with pytest.raises(KeyError, match='_primary_taxable_income'):
        simulate_year_pure(state=state, year=0, inputs=_build_year_inputs(
            allocations={'_primary_income': 150_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, year_brackets=B))


def test_missing_income_is_not_read_when_nothing_to_deduct():
    """Laziness is real: with nothing in the ledger, no income is needed."""
    cfg = _config()
    r0, _ = simulate_year_pure(state=SimState.initial(cfg), year=0,
                               inputs=_build_year_inputs(
                                   allocations={'_primary_income': 150_000,
                                                '_annual_savings': 0},
                                   config=cfg, investment_return=0.0,
                                   year_brackets=B))
    assert r0.rrsp_tax_savings == 0.0


# ── INV-11: no opinion defaults in the deduct-later target (unit tests) ─────

def test_deferred_fallback_derived_from_brackets():
    """UNIT test: income in the FIRST bracket with no declared target. The
    old fallback (brackets[3]['min'] or a hard-coded 50,000) put the target
    ABOVE the income and claimed nothing; the target is now the floor of the
    bracket the income sits in, from the loaded brackets."""
    ledger = RRSPListLedger()
    ledger.add_contribution(year=0, amount=30_000, role='primary')
    claim = ledger.claim_deferred_deduction(year=0, income=40_000, brackets=B,
                                            bracket_target=0.0)
    assert abs(claim['amount'] - 30_000) < TOL
    assert abs(claim['savings'] - deduction_value(40_000, 30_000, B)) < TOL
    with pytest.raises(ValueError):
        RRSPListLedger().claim_deferred_deduction(year=0, income=40_000,
                                                  brackets=[], bracket_target=0.0)


def test_bracket_floor_helpers_fail_loudly():
    """UNIT test of the two pure helpers."""
    assert current_bracket_floor(40_000, B) == B[0]['min']
    assert current_bracket_floor(150_000, B) == 132_245
    # At a bracket's upper edge the deduction first removes income from
    # THAT bracket.
    assert current_bracket_floor(132_245, B) == 117_045
    assert lowest_taxed_floor(B) == B[0]['min']
    with pytest.raises(ValueError):
        lowest_taxed_floor([])
    with pytest.raises(ValueError):
        lowest_taxed_floor([{'min': 0, 'max': 0, 'rate': 0.0}])
    with pytest.raises(ValueError):
        current_bracket_floor(40_000, [])
    zero_band = [{'min': 0, 'max': 16_000, 'rate': 0.0},
                 {'min': 16_000, 'max': 0, 'rate': 0.30}]
    assert lowest_taxed_floor(zero_band) == 16_000


# ── INV-17: the carry-forward disclosure, on engine results ─────────────────

def test_carry_forward_disclosure():
    from model_fidelity import active_approximations

    cfg = _config()
    r0, s1 = _step(SimState.initial(cfg), 0, cfg, own=200_000)
    r1, _ = _step(s1, 1, cfg)
    summary = summarize_rrsp_deduction_carry_forward([r0, r1])
    assert summary['engaged'] is True
    assert summary['first_carried_year'] == r0.year
    assert abs(summary['first_carried_amount'] - 50_000) < TOL
    assert abs(summary['max_carried_forward'] - 50_000) < TOL
    assert summary['carried_at_horizon_end'] == 0.0

    clean, _ = _step(SimState.initial(cfg), 0, cfg, own=5_000)
    clear = summarize_rrsp_deduction_carry_forward([clean])
    assert clear['engaged'] is False

    worst = worst_rrsp_deduction_carry_forward([clear, summary, None])
    assert worst == summary
    assert worst_rrsp_deduction_carry_forward([clear])['engaged'] is False

    by_id = {a.id: a for a in active_approximations(
        {'assumptions': {'rrsp_deduction_carried_forward': summary}})}
    assert 'rrsp_deduction_carried_forward' in by_id
    from model_fidelity import FidelityContext
    text = ' '.join(by_id['rrsp_deduction_carried_forward'].findings(
        FidelityContext(cfg={'assumptions': {
            'rrsp_deduction_carried_forward': summary}})))
    assert f"year {r0.year}" in text and '$50,000' in text

    ids_clear = {a.id for a in active_approximations(
        {'assumptions': {'rrsp_deduction_carried_forward': clear}})}
    assert 'rrsp_deduction_carried_forward' not in ids_clear


def test_horizon_end_carry_is_named():
    from model_fidelity import FidelityContext, all_approximations
    approx = {a.id: a for a in all_approximations()}['rrsp_deduction_carried_forward']
    summary = {'engaged': True, 'first_carried_year': 16,
               'first_carried_amount': 3_000.0, 'max_carried_forward': 6_000.0,
               'carried_at_horizon_end': 6_000.0}
    text = ' '.join(approx.findings(FidelityContext(cfg={'assumptions': {
        'rrsp_deduction_carried_forward': summary}})))
    assert '$6,000 was still undeducted at the end of the horizon' in text


# ── INV-4: the refund never exceeds the tax it reduces, even when the
# taxable base is below gross income (an s.20(1)(c) loan deduction) ─────────

def test_refund_capped_at_taxable_income_with_interest_deduction():
    """Primary earns $20k and deducts $5k of investment-loan interest: the
    TAXABLE income is $15k. A $30k contribution can only remove tax on that
    $15k -- valued against the $20k gross it would exceed the pre-credit tax
    actually payable. Driven through FamilySimulation.run (the real
    prologue computes the taxable base)."""
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    from strategy import AllocationResult

    members = [
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 20_000,
         'id': 'p1', 'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 0},
        {'role': 'spouse', 'birth_year': 1982, 'gross_income': 40_000,
         'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]
    loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1', 'rate': 0.05,
            'principal': 100_000, 'use': 'investment',
            'repayment': 'amortizing', 'interest': 'paid'}
    cfg = SimulationConfig(
        projection_years=1, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, living_costs=0, start_year=2026,
        province='quebec', investment_return=0.0, salary_growth=0.0,
        family_members=members, children=[], private_loans=[loan])
    with mock.patch('strategy.StrategyEngine.allocate',
                    return_value=AllocationResult(primary_rrsp=30_000.0)):
        r0 = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()[0]
    taxable = 20_000 - 0.05 * 100_000
    assert abs(r0.rrsp_tax_savings - tax_on_income(taxable, B)) < 1e-3
    assert r0.rrsp_tax_savings < tax_on_income(20_000, B)
    assert abs(r0.rrsp_deduction_carried_forward - (30_000 - taxable)) < 1e-3
