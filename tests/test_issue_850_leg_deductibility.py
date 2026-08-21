"""s.20(1)(c) deductibility on the mortgage ADVANCE and the DRAWN line (#850).

## What #850 found, and what this file pins

#849 states the household's notary-day question as a DEDUCTIBILITY trade-off:

  > take the surplus as a mortgage advance (cheaper rate, but forced
  > amortization erodes the deductible balance) or draw it from the
  > readvanceable line (dearer, but interest-only, permanently deductible)

Both halves of that sentence are about deductibility, and #850 found the engine
modelled it on NEITHER leg: ``config.cash_out``'s only consumers SIZE the
invested lump sum, and ``apply_margin_heloc_interest`` capitalized/serviced but
never deducted. The only deductible interest was ``apply_sm_interest``, gated on
``use_readvanceable`` and computed on the distinct ``new_sm_heloc`` balance. So
the advance-vs-line ranking was decided by the rate gap and interest
capitalization alone -- a confident number for a question nobody asked (DP#32).

The load-bearing test here is ``TestTheErosion``: the advance's deductible
balance falls with amortization, the line's does not. That asymmetry IS #849's
question, and before #850 both legs were flat at zero, so the instrument could
not have told the two apart.

DP#15: every household below is fabricated, round-numbered, role-named.
"""

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (borrowing_purpose_tracings,
                              compute_heloc_deductible_proportion,
                              margin_draw_for_lump_sum)


# ── The fabricated household (DP#15) ────────────────────────────────────────
# House 800,000; the charge is refinanced to 640,000 (80% LTV) against an
# existing 160,000 mortgage, so the SURPLUS is 480,000. That surplus is the
# thing #849 asks where to source from.
HOUSE_VALUE = 800_000
CHARGE = 640_000
EXISTING_MORTGAGE = 160_000
SURPLUS = 480_000
ADVANCE_RATE = 0.0370   # amortizing, cheaper
LINE_RATE = 0.0470      # revolving, interest-only, dearer


def _sourced(revolving_share: float) -> tuple:
    """The (mortgage_balance, margin_available) the household ends up with when
    it sources the surplus with ``revolving_share`` of the charge revolving.

    This is ``apply_sourcing_overlay``'s arithmetic (PR #852), reproduced here
    ONLY as a fixture: that function lives on #845's branch and is not on main
    yet, and #850 is about what the ENGINE prices, not about the composition.
    Money is conserved at every share by construction:
    ``mortgage + line_draw == CHARGE``, and the invested surplus is
    ``SURPLUS``, once.
    """
    revolving = CHARGE * revolving_share
    line_draw = min(SURPLUS, revolving)
    return CHARGE - line_draw, revolving


def _household(revolving_share: float, non_reg_yield: float = 0.02) -> dict:
    mortgage_balance, margin_available = _sourced(revolving_share)
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
                 'retirement_age': 65,
                 # No registered room: the whole borrowed surplus must land in
                 # the NON-REGISTERED account, which is the income-producing
                 # (hence s.20(1)(c)-qualifying) use. A household with room
                 # would see part of the advance traced to a sheltered plan
                 # and correctly NOT deducted -- that case is pinned by
                 # TestOnlyIncomeProducingUseQualifies below.
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'accounts': {'rrsp_annual_max': 0},
        'assumptions': {
            'start_year': 2026, 'horizon_age': 60,
            'investment_return': 0.06, 'salary_growth': 0.0,
            'inflation': 0.0, 'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 0, 'cost_basis': 0,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': non_reg_yield, 'interest': 0.0},
                },
            },
        },
        'property': {
            'house_value': HOUSE_VALUE,
            'mortgage_balance': mortgage_balance,
            'mortgage_rate': ADVANCE_RATE,
            'heloc_rate': LINE_RATE,
            'margin_available': margin_available,
            'heloc_readvance': False,
            'amortization_years': 25,
        },
        'household_budget': {'annual_living_costs': 60_000},
    }


def _run(cfg: dict, lump_sum: float = SURPLUS):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=lump_sum)
    return sim.run()


# ============================================================================
# 1. The tracing splits the borrowing without inventing or losing a dollar
# ============================================================================

class TestMoneyIsConserved:
    """DP#18: the two legs must partition the borrowing EXACTLY -- PR #852 hit
    the double-count trap from the other side (investing the same borrowed
    dollar twice), and a tracing that double-counts would deduct it twice."""

    @pytest.mark.parametrize('share', [0.0, 0.1, 0.25, 0.5, 0.75, 1.0])
    def test_the_two_legs_partition_the_lump_sum_at_every_share(self, share):
        """Method: for each share, sum the two legs' traced INVESTMENT advances
        and compare to the invested lump. Not one borrowed dollar may be
        traced to both the advance and the line, or to neither."""
        mortgage_balance, margin_available = _sourced(share)
        advance, margin = borrowing_purpose_tracings(
            lump_sum=SURPLUS, lump_non_reg=SURPLUS,
            margin_available=margin_available, mortgage_balance=mortgage_balance)
        traced = advance['investment_advances'] + margin['investment_advances']
        assert traced == pytest.approx(SURPLUS), (
            f"at revolving_share {share} the two legs traced ${traced:,.0f} of "
            f"investment borrowing but only ${SURPLUS:,.0f} was borrowed and "
            f"invested -- a borrowed dollar is being deducted twice, or lost")

    @pytest.mark.parametrize('share', [0.0, 0.5, 0.75])
    def test_the_split_reuses_the_rule_that_books_the_debt(self, share):
        """DP#9: the tracing must not re-derive which borrowing is which. The
        margin leg's total MUST equal ``margin_draw_for_lump_sum`` -- the same
        function ``initial_state_for_run`` books ``heloc_balance`` from -- so
        the debt on the balance sheet and the debt being deducted are the same
        debt by construction, not by coincidence."""
        mortgage_balance, margin_available = _sourced(share)
        _advance, margin = borrowing_purpose_tracings(
            lump_sum=SURPLUS, lump_non_reg=SURPLUS,
            margin_available=margin_available, mortgage_balance=mortgage_balance)
        assert margin['total_advances'] == margin_draw_for_lump_sum(
            SURPLUS, margin_available)

    def test_the_advance_is_traced_against_the_whole_blended_mortgage(self):
        """The pre-existing 160,000 was borrowed to buy a HOUSE -- a personal
        purpose. So a pure-advance sourcing must deduct only 480,000/640,000 of
        the mortgage's interest, never all of it: the deductible PROPORTION is
        investment over the WHOLE blended borrowing (CRA IT-533 pro rata)."""
        advance, _margin = borrowing_purpose_tracings(
            lump_sum=SURPLUS, lump_non_reg=SURPLUS,
            margin_available=0.0, mortgage_balance=CHARGE)
        assert advance['total_advances'] == CHARGE
        assert advance['investment_advances'] == SURPLUS
        assert compute_heloc_deductible_proportion(
            advance, yield_rate=0.02) == pytest.approx(SURPLUS / CHARGE)


# ============================================================================
# 2. Purpose: only an income-producing use qualifies
# ============================================================================

class TestOnlyIncomeProducingUseQualifies:
    """s.20(1)(c) has TWO tests, and the tracing must reuse both -- they are
    already implemented in ``compute_heloc_deductible_proportion`` (DP#9)."""

    def test_a_lump_that_filled_registered_room_is_not_deductible(self):
        """Interest on money borrowed to fill an RRSP/TFSA is NOT deductible:
        the plan's income is sheltered. A tracing that ignored the purpose test
        would hand this household a deduction it cannot claim."""
        advance, margin = borrowing_purpose_tracings(
            lump_sum=SURPLUS, lump_non_reg=0.0,   # all of it went registered
            margin_available=CHARGE * 0.75, mortgage_balance=EXISTING_MORTGAGE)
        assert compute_heloc_deductible_proportion(advance, yield_rate=0.02) == 0.0
        assert compute_heloc_deductible_proportion(margin, yield_rate=0.02) == 0.0

    def test_a_zero_yield_holding_fails_the_income_test(self):
        """A pure-growth, zero-yield holding has no reasonable expectation of
        income, so the interest does not qualify federally -- regardless of how
        cleanly the advance traces. Reused from the readvance line's own rule,
        not reimplemented."""
        advance, _margin = borrowing_purpose_tracings(
            lump_sum=SURPLUS, lump_non_reg=SURPLUS,
            margin_available=0.0, mortgage_balance=CHARGE)
        assert compute_heloc_deductible_proportion(advance, yield_rate=0.0) == 0.0

    def test_no_lump_sum_traces_no_borrowing(self):
        """DP#32: a household that borrowed nothing gets a hard zero, never a
        fabricated advance. This is the gate the golden household passes
        through every year of its 46-year horizon."""
        advance, margin = borrowing_purpose_tracings(
            lump_sum=0.0, lump_non_reg=0.0,
            margin_available=150_000, mortgage_balance=400_000)
        assert advance['investment_advances'] == 0.0
        assert margin['investment_advances'] == 0.0


# ============================================================================
# 3. THE DELIVERABLE: the erosion #849 names, measured
# ============================================================================

class TestTheErosion:
    """#850's acceptance criterion: 'A test measures the erosion: the advance's
    deductible balance falls with amortization; the line's does not.'

    This is the asymmetry the whole question turns on. Before #850 BOTH legs
    were flat at zero every year, so no instrument in this repo could have told
    an advance from a line on the grounds the household actually cares about.
    """

    def test_the_advances_deductible_balance_erodes_with_the_principal(self):
        """Method: source the whole surplus as an ADVANCE (revolving_share 0),
        run the projection, and read advance_deductible_balance year over year.
        The mortgage amortizes, and the deductible proportion is fixed, so the
        deductible balance must FALL every single year."""
        rs = _run(_household(revolving_share=0.0))
        balances = [r.advance_deductible_balance for r in rs]
        assert balances[0] > 0, (
            "the advance's deductible balance is zero -- #850's finding "
            "(cash_out reaches no deduction rule) has regressed")
        for earlier, later in zip(balances, balances[1:]):
            assert later < earlier, (
                f"the advance's deductible balance did not erode: {earlier:,.0f} "
                f"-> {later:,.0f}. Forced principal repayment MUST shrink it "
                f"(#849) -- that is the cost of taking the cheaper rate.")

    def test_the_drawn_lines_deductible_balance_does_not_erode(self):
        """Method: source the whole surplus from the LINE (revolving_share
        0.75 => revolving room 480,000 == the surplus, so it is drawn in
        full), run, and read margin_deductible_balance year over year. The line
        is interest-only, so its deductible balance must NOT fall."""
        rs = _run(_household(revolving_share=0.75))
        balances = [r.margin_deductible_balance for r in rs]
        assert balances[0] > 0, (
            "the drawn line's deductible balance is zero -- #850's finding "
            "(apply_margin_heloc_interest never deducts) has regressed")
        for earlier, later in zip(balances, balances[1:]):
            assert later >= earlier, (
                f"the drawn line's deductible balance FELL: {earlier:,.0f} -> "
                f"{later:,.0f}. An interest-only revolving balance does not "
                f"amortize -- not eroding is precisely what is bought with the "
                f"dearer rate (#849).")

    def test_the_two_legs_diverge(self):
        """The single sentence #849 is built on, as one assertion: over the
        same horizon, at the same charge, on the same invested surplus, the
        advance's deductible balance shrinks by more than the line's."""
        advance_rs = _run(_household(revolving_share=0.0))
        line_rs = _run(_household(revolving_share=0.75))
        advance_decay = (advance_rs[-1].advance_deductible_balance
                         / advance_rs[0].advance_deductible_balance)
        line_decay = (line_rs[-1].margin_deductible_balance
                      / line_rs[0].margin_deductible_balance)
        assert advance_decay < line_decay, (
            f"advance kept {advance_decay:.1%} of its deductible balance, line "
            f"kept {line_decay:.1%} -- the advance must erode faster; that "
            f"asymmetry IS the question (#849)")


# ============================================================================
# 4. The deduction is actually priced (the #850 regression pins)
# ============================================================================

class TestBothLegsAreNowDeducted:
    """#850: 'Neither leg's interest is deductible in this engine.' These pin
    that this is no longer true -- and that the deduction reaches the RANKING,
    not merely a report column."""

    def test_the_advance_accrues_deductible_interest(self):
        rs = _run(_household(revolving_share=0.0))
        assert sum(r.advance_deductible_interest for r in rs) > 0
        assert sum(r.margin_deductible_interest for r in rs) == 0, (
            "a pure-advance sourcing drew no line, so no line interest can be "
            "deducted -- money that was never borrowed cannot be deducted")

    def test_the_drawn_line_accrues_deductible_interest(self):
        rs = _run(_household(revolving_share=0.75))
        assert sum(r.margin_deductible_interest for r in rs) > 0
        assert sum(r.advance_deductible_interest for r in rs) == 0, (
            "the whole surplus came from the line, so the mortgage is back to "
            "its original personal-purpose balance -- nothing to deduct")

    def test_the_deduction_reaches_net_benefit(self):
        """DP#32/#850's own framing: a deduction the ranking cannot see is a
        trade-off the engine did not compute. ``compute_net_benefit`` must
        carry the traced savings, or advance-vs-line is still ranked on the
        rate gap alone."""
        from optimize import compute_net_benefit
        cfg = _household(revolving_share=0.0)
        rs = _run(cfg)
        saved = sum(r.traced_borrowing_tax_savings for r in rs)
        assert saved > 0
        with_deduction = compute_net_benefit(rs, cfg)
        stripped = [_zeroed(r) for r in rs]
        assert with_deduction - compute_net_benefit(stripped, cfg) == pytest.approx(saved)


def _zeroed(result):
    """A copy of ``result`` with the #850 savings removed -- i.e. the pre-#850
    world, where the ranking priced rate and capitalization only."""
    from copy import copy
    stripped = copy(result)
    stripped.traced_borrowing_tax_savings = 0.0
    return stripped


# ============================================================================
# 5. The scope limit this fix INHERITS, measured rather than described
# ============================================================================

class TestTheSharedQcCapIsAScopeLimit:
    """#850 says to deduct the two new legs "on the same rule the readvance
    line already uses (DP#9 -- not a fourth copy)". Doing so inherits that
    rule's pre-existing conflation, and this pins it rather than letting it
    hide: ``apply_sm_interest`` applies the QUEBEC investment-expense cap (TA
    s.336.0.1: investment expenses limited to investment income) and then
    values the result at ``primary_marginal_rate``, a COMBINED federal+Quebec
    rate. But the FEDERAL s.20(1)(c) deduction has no investment-income
    limitation at all. So a leg whose interest exceeds its investment income is
    under-deducted federally.

    That is not a defect this fix introduces, and not one to fix inside an
    issue about tracing -- but it is NOT neutral between the two legs, and
    saying so is the point (DP#32). The LINE does not amortize, so it accrues
    more interest for longer and the cap binds on it; the ADVANCE erodes, so
    its interest stays under the cap. The conflation therefore penalizes
    exactly the leg #849 expects to benefit.

    Measured below so the advance-vs-line conclusion can be stated with a
    BOUND on it, instead of resting on an unexamined cap.
    """

    def test_the_cap_binds_on_the_line_and_strands_carry_forward(self):
        """Method: sum the line's deductible interest over the horizon and
        compare to what the shared QC cap actually let it claim."""
        rs = _run(_household(revolving_share=0.75))
        deductible = sum(r.margin_deductible_interest for r in rs)
        claimed = sum(r.sm_qc_deductible for r in rs)
        assert claimed < deductible, (
            "premise check: this fixture is supposed to demonstrate a BINDING "
            "QC cap on the line -- if it no longer binds, this test is no "
            "longer measuring the scope limit it claims to measure")
        assert rs[-1].sm_qc_carry_forward > 0, (
            "the interest the cap refused must survive as carry-forward, not "
            "vanish -- it is a deduction deferred, not a deduction denied")

    def test_the_cap_does_not_bind_on_the_amortizing_advance(self):
        """The asymmetry that makes the conflation non-neutral: the advance's
        interest erodes, so it stays inside the cap and is fully claimed."""
        rs = _run(_household(revolving_share=0.0))
        deductible = sum(r.advance_deductible_interest for r in rs)
        claimed = sum(r.sm_qc_deductible for r in rs)
        assert claimed == pytest.approx(deductible), (
            "the advance's deduction is expected to fit inside the cap; if it "
            "no longer does, the bound quoted on #850's conclusion is stale")


# ============================================================================
# 6. DP#32: absence is a no-op
# ============================================================================

class TestAbsenceIsANoOp:
    def test_a_household_that_borrowed_nothing_deducts_nothing(self):
        """The golden gate, stated locally: no lump sum => both legs are hard
        zeros in every year. (The golden invariant itself is pinned bit-exact
        by tests/test_issue_759_installment_obligation.py.)"""
        rs = _run(_household(revolving_share=0.0), lump_sum=0.0)
        assert all(r.advance_deductible_balance == 0.0 for r in rs)
        assert all(r.margin_deductible_balance == 0.0 for r in rs)
        assert all(r.traced_borrowing_tax_savings == 0.0 for r in rs)
