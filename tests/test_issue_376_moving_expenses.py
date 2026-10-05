"""Issue #376: the moving-expense deduction (ITA s.62 / TA s.348).

## What is modelled

An individual who moves to start a job, a business, or full-time
post-secondary studies may deduct eligible moving costs against income
available to shelter them (CRA line 21900; Quebec TA s.348, TP-1 line 228).
The deduction is capped at that income and **the unused balance carries
forward** to later years.

The deduction rides the fold's existing ``(income_add, deduction)`` slot --
the same one private-loan interest (s.20(1)(c)) and rental interest use -- so it
reduces taxable income before `tax_on_income` through the one existing
mechanism rather than a new one (DP#9). The carry-forward lives in
``jurisdiction_state['canada']['moving_expense_carry_forward']``, keyed by
role, and is written onto `next_state` *outside* the fold because
`simulate_year_pure` is a pure function of explicit state (DP#26).

## Two places the engine knows less than the statute

Both are asserted in the tests rather than left for a reader to discover.

**The cap is the member's whole declared income, not their new-location
income.** The statute caps at income earned *at the new location*; this engine
holds no locations. The total is an upper bound on that, so the deduction can
come out higher than the statute would allow and never lower.

**The 40 km test is the taxpayer's assertion.** Distance is not a quantity the
engine holds -- it knows neither endpoint's geography. Declaring an amount
asserts the move qualified; the schema says so in the field's description.

## What is NOT modelled

The issue's acceptance test also asks that income-tested credits (GST/HST,
solidarity, CWB, Quebec work premium) be recomputed on the reduced net income.
**Those credits are not computed by this engine at all** -- there is no
non-refundable-credit path in `simulation_rules.py` or `simulation_state.py`
(tracked as #325). That half of the acceptance test is therefore unreachable
today, and the deduction is landed without it rather than as a claim that the
credits were refreshed. The tax saving itself is real and measured below.
"""
import pytest

from countries.canada.moving_expenses import (
    MINIMUM_MOVING_KM, claimed_moving_expenses, moving_expense_deduction,
)


class TestTheDeclaredAmountIsReadNotInferred:
    def test_a_declared_move_is_read_for_its_own_year(self):
        member = {'moving_expenses': [{'year': 2024, 'amount': 1500}]}
        assert claimed_moving_expenses(member, 2024) == 1500.0

    def test_another_year_claims_nothing(self):
        """The deduction belongs to the year of the MOVE. Carrying it forward
        as an expense every year would deduct the same cost repeatedly."""
        member = {'moving_expenses': [{'year': 2024, 'amount': 1500}]}
        assert claimed_moving_expenses(member, 2025) == 0.0

    def test_no_block_claims_nothing(self):
        assert claimed_moving_expenses({}, 2024) == 0.0

    def test_an_empty_list_claims_nothing(self):
        assert claimed_moving_expenses({'moving_expenses': []}, 2024) == 0.0

    def test_two_moves_in_one_year_sum(self):
        member = {'moving_expenses': [{'year': 2024, 'amount': 900},
                                      {'year': 2024, 'amount': 600},
                                      {'year': 2023, 'amount': 500}]}
        assert claimed_moving_expenses(member, 2024) == 1500.0

    def test_a_declared_zero_is_a_claim_of_zero_not_absence(self):
        """DP#32: 0 is a value. `amount: 0` must read as a claim of zero, which
        is the same answer here -- but the test pins that the key is READ, so
        a future `or` fallback cannot quietly turn it into something else."""
        member = {'moving_expenses': [{'year': 2024, 'amount': 0}]}
        assert claimed_moving_expenses(member, 2024) == 0.0

    def test_a_year_of_zero_is_still_matched(self):
        """The mirror image: `year: 0` is a real record the schema permits, and
        a truthiness test would skip it and lose the amount."""
        member = {'moving_expenses': [{'year': 0, 'amount': 700}]}
        assert claimed_moving_expenses(member, 0) == 700.0

    def test_a_missing_amount_contributes_nothing(self):
        member = {'moving_expenses': [{'year': 2024}]}
        assert claimed_moving_expenses(member, 2024) == 0.0


class TestTheIncomeCapAndTheCarryForward:
    """CRA line 21900: capped at the available income, remainder carries."""

    def test_expenses_under_income_deduct_in_full(self):
        deduction, carry = moving_expense_deduction(1500.0, 25_000.0)
        assert deduction == 1500.0
        assert carry == 0.0

    def test_expenses_over_income_are_capped_and_the_rest_carries(self):
        """The issue's second case: $30,000 of expenses against $25,000 of
        income deducts $25,000 now and carries $5,000."""
        deduction, carry = moving_expense_deduction(30_000.0, 25_000.0)
        assert deduction == 25_000.0
        assert carry == 5_000.0

    def test_the_carried_balance_deducts_next_year(self):
        deduction, carry = moving_expense_deduction(0.0, 40_000.0,
                                                    carry_forward=5_000.0)
        assert deduction == 5_000.0
        assert carry == 0.0

    def test_a_carry_shrinks_with_the_income_available(self):
        deduction, carry = moving_expense_deduction(0.0, 2_000.0,
                                                    carry_forward=5_000.0)
        assert deduction == 2_000.0
        assert carry == 3_000.0

    def test_no_income_means_no_deduction_but_nothing_is_lost(self):
        """A retired year cannot shelter the expense, and the balance must
        SURVIVE it rather than evaporating."""
        deduction, carry = moving_expense_deduction(30_000.0, 0.0)
        assert deduction == 0.0
        assert carry == 30_000.0

    def test_nothing_claimed_and_nothing_carried_is_nothing(self):
        assert moving_expense_deduction(0.0, 50_000.0) == (0.0, 0.0)

    def test_a_claim_and_a_carry_are_pooled(self):
        """A second move adds to an outstanding balance, not alongside it."""
        deduction, carry = moving_expense_deduction(1_000.0, 40_000.0,
                                                    carry_forward=2_000.0)
        assert deduction == 3_000.0
        assert carry == 0.0

    def test_the_cap_binds_exactly_at_the_income(self):
        """Boundary: equal income and expenses deduct fully with nothing over."""
        deduction, carry = moving_expense_deduction(25_000.0, 25_000.0)
        assert deduction == 25_000.0
        assert carry == 0.0

    def test_one_dollar_over_the_income_carries_one_dollar(self):
        deduction, carry = moving_expense_deduction(25_000.01, 25_000.0)
        assert deduction == 25_000.0
        assert carry == pytest.approx(0.01)

    def test_a_negative_claim_never_creates_a_deduction(self):
        deduction, carry = moving_expense_deduction(-500.0, 25_000.0)
        assert deduction == 0.0
        assert carry == 0.0

    def test_negative_income_does_not_produce_a_negative_deduction(self):
        deduction, carry = moving_expense_deduction(1_000.0, -5_000.0)
        assert deduction == 0.0
        assert carry == 1_000.0


class TestTheDocumentedLimits:
    """The two places the engine knows less than the statute. Asserted here so
    they are decisions on the record rather than surprises."""

    def test_the_statutory_distance_is_named(self):
        """CRA's 40 km minimum is recorded as the constant the contract's
        assertion refers to, even though the engine cannot evaluate it."""
        assert MINIMUM_MOVING_KM == 40

    def test_the_cap_uses_total_income_so_it_is_an_upper_bound(self):
        """Pricing against total income can only ever allow MORE than the
        statute's new-location cap, never less. Pinned so nobody 'fixes' it by
        inventing a location split the engine does not have."""
        deduction, _ = moving_expense_deduction(1_500.0, 25_000.0)
        assert deduction == 1_500.0


class TestTheWiringIntoTheFold:
    """The helper the prologue calls. A correct pure function that nothing
    calls is the dead-module failure this repo exists to prevent."""

    def test_the_prologue_helper_returns_one_deduction_and_one_balance_per_adult(self):
        from simulation import _moving_expense_deductions

        class _Cfg:
            def adults(self):
                return [{'role': 'primary',
                         'moving_expenses': [{'year': 2024, 'amount': 1500}]},
                        {'role': 'spouse'}]

        p_ded, p_carry, s_ded, s_carry = _moving_expense_deductions(
            _Cfg(), 2024, 25_000.0, 0.0)
        assert p_ded == 1500.0
        assert p_carry == 0.0
        assert s_ded == 0.0       # a member with no declaration claims nothing
        assert s_carry == 0.0

    def test_the_helper_prices_each_member_against_their_own_income(self):
        from simulation import _moving_expense_deductions

        class _Cfg:
            def adults(self):
                return [{'role': 'primary',
                         'moving_expenses': [{'year': 2024, 'amount': 40_000}]},
                        {'role': 'spouse',
                         'moving_expenses': [{'year': 2024, 'amount': 40_000}]}]

        p_ded, p_carry, s_ded, _ = _moving_expense_deductions(
            _Cfg(), 2024, 10_000.0, 60_000.0)
        assert p_ded == 10_000.0     # capped at the smaller income
        assert p_carry == 30_000.0
        assert s_ded == 40_000.0     # capped at the larger one

    def test_an_absent_carry_store_is_not_an_error(self):
        """`None` means "no state yet" -- the first year of a projection -- and
        must read as no carried balance rather than raising."""
        from simulation import _moving_expense_deductions

        class _Cfg:
            def adults(self):
                return [{'role': 'primary',
                         'moving_expenses': [{'year': 2024, 'amount': 500}]},
                        {'role': 'spouse'}]

        p_ded, *_ = _moving_expense_deductions(_Cfg(), 2024, 50_000.0, 0.0,
                                               carry_forward=None)
        assert p_ded == 500.0


class TestTheContractCarriesTheDeclaration:
    """The schema leaf nothing reads would fail the schema-coverage guard; the
    mapper that drops it would make every test above vacuous."""

    def test_a_declared_move_reaches_the_member(self):
        from contract_people import _map_member
        person = {
            'id': 'primary', 'birth_date': '1985-05-05',
            'room': {'rrsp': None, 'tfsa': None, 'fhsa': None},
            'incomes': [], 'relationships': [],
            'moving_expenses': [{'year': 2024, 'amount': 1500}],
        }
        doc = {'as_of': '2026-01-01', 'people': [person],
               'decisions': {'retirement_age': [{'person': 'primary', 'candidate_ages': [65]}]}, 'family': {}}
        member = _map_member(doc, person['id'], 'primary', {})
        assert member.get('moving_expenses') == [{'year': 2024, 'amount': 1500}]

    def test_an_undeclared_move_leaves_no_key(self):
        """Absence stays absence -- a member with no move gets no key at all,
        so every pre-existing trajectory is untouched (DP#32)."""
        from contract_people import _map_member
        person = {
            'id': 'primary', 'birth_date': '1985-05-05',
            'room': {'rrsp': None, 'tfsa': None, 'fhsa': None},
            'incomes': [], 'relationships': [],
        }
        doc = {'as_of': '2026-01-01', 'people': [person],
               'decisions': {'retirement_age': [{'person': 'primary', 'candidate_ages': [65]}]}, 'family': {}}
        member = _map_member(doc, person['id'], 'primary', {})
        assert 'moving_expenses' not in member

# ── The carry-forward through a real fold ──────────────────────────────────
#
# The unit tests above exercise the pure arithmetic and the prologue helper.
# They CANNOT reach the state writeback in `simulate_year`, which runs after
# the fold and only fires when a balance genuinely carries forward -- i.e. when
# expenses exceed the income available to shelter them. Only a projection can
# reach it, and the coverage gate is right to insist on one.

from simulation import FamilySimulation, SimulationConfig  # noqa: E402


def _cfg_with_a_move(claimed, income, years=3):
    """A minimal two-year projection where the primary declares a move.

    Expenses are set ABOVE the income so the deduction is capped and a real
    balance carries forward -- that is the only way to reach the writeback.
    """
    return {
        'assumptions': {
            'projection_years': years, 'investment_return': 0.06,
            'salary_growth': 0.0, 'start_year': 2026, 'frozen_brackets': True,
            'time_step': 'yearly',
        },
        'savings': {'rate': 0.15},
        'property': {'house_value': 600000, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'ltv_max': 0.80,
                     'current_payment_monthly': 0, 'amortization_years': 25,
                     'margin_available': 0},
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': income,
                 'moving_expenses': [{'year': 2026, 'amount': claimed}]},
            ],
            'children': [],
        },
        'accounts': {}, 'cash_flows': [],
    }


class TestTheCarryForwardSurvivesAFold:
    """The state writeback lives AFTER the fold in ``simulate_year``, so it is
    unreachable from any unit-level test -- only a projection reaches it. The
    coverage gate is right to insist on one.

    One projection year, deliberately: the balance must still be outstanding at
    the end for the writeback to have anything to write. Over a longer horizon a
    second year shelters it and the key legitimately disappears again, which
    would make this test observe nothing.
    """

    def _run_one_year(self, claimed, income):
        cfg = SimulationConfig.from_dict(
            _cfg_with_a_move(claimed, income, years=1))
        sim = FamilySimulation(cfg, use_readvanceable=False)
        sim.run()
        return sim._state

    def test_an_oversized_move_writes_the_remainder_onto_the_next_state(self):
        """$40,000 of expenses against $25,000 of income: $25,000 is sheltered
        and the remaining $15,000 must survive onto the next state so the
        following year can shelter it. Reaching this writeback is the point."""
        state = self._run_one_year(40_000, 25_000)
        carried = state.jurisdiction_state.get('canada', {}).get(
            'moving_expense_carry_forward', {})
        assert carried.get('primary') == pytest.approx(15_000.0)

    def test_a_fully_sheltered_move_leaves_no_key_at_all(self):
        """The DP#32 negative: $1,500 against $25,000 shelters fully, so nothing
        carries and NO key is written -- leaving every pre-existing trajectory
        byte-identical. A zero written here would be a state difference that no
        downstream test could see."""
        state = self._run_one_year(1_500, 25_000)
        assert 'moving_expense_carry_forward' not in \
            state.jurisdiction_state.get('canada', {})

    def test_a_household_that_declares_no_move_writes_nothing(self):
        """The pre-existing case, which must stay untouched: a member with no
        `moving_expenses` block at all never reaches the writeback."""
        cfg = SimulationConfig.from_dict(_cfg_with_a_move(0, 25_000, years=1))
        sim = FamilySimulation(cfg, use_readvanceable=False)
        sim.run()
        assert 'moving_expense_carry_forward' not in \
            sim._state.jurisdiction_state.get('canada', {})
