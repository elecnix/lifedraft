"""Issue #371, engine half: the student-loan-interest credits reach a real run.

Everything here drives ``FamilySimulation.run`` and reads ``YearResult``. The
pure law is pinned in the same file's sibling test class; these assert that the
rule is wired, that it lands on the right member's return, and that the cash-flow
identity sees the reduction.

The acceptance figures are the issue's, adjusted to the year the fold actually
projects: the federal lowest rate is year-versioned data (15% for 2023-2024,
14.5% for 2025, 14% for 2026). The issue's $31.31 is the 2024 figure; a 2026
projection earns 250 x 14% x (1 - 16.5%) = $29.225 federal, plus $50.00 Quebec.
Asserting the issue's 15%-era number against a 2026 run would pin a stale rate.

DP#15: fabricated, round-numbered fixtures and role-based names.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig

BALANCE = 5_000.0
RATE = 0.05
INTEREST = BALANCE * RATE          # 250.0, the issue's figure
PROVINCE = 'qc'
LIVING_COSTS = 50_000.0            # so the solvency identity + after_tax_income fire


def _cfg(qualifying=True, province=PROVINCE, income=120_000, with_loan=True,
         balance=BALANCE, rate=RATE, projection_years=6, time_step='yearly'):
    member = {
        'role': 'primary', 'id': 'p1', 'birth_year': 1980,
        'retirement_age': 65, 'gross_income': income,
        'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0,
    }
    loans = []
    if with_loan:
        loans.append({
            'id': 'student_a', 'kind': 'student_loan', 'owner': 'p1',
            'balance': balance, 'rate': rate, 'payment_monthly': 100.0,
            'amortization_years': 10,
            # Issue #371: schema-required and mapper-refused when absent.
            'qualifying_government_loan': qualifying,
        })
    return {
        'family': {'members': [member], 'children': []},
        'consumer_loans': loans,
        'assumptions': {'start_year': 2026, 'projection_years': projection_years,
                        'investment_return': 0.0, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True,
                        'time_step': time_step},
        'savings': {'rate': 0.0},
        'tax': {'province': province},
        'household_budget': {'living_costs': LIVING_COSTS},
    }


def _run_from(cfg_dict):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def _run(**kwargs):
    return _run_from(_cfg(**kwargs))


class TestTheCreditReachesTheEngine:
    def test_a_qualifying_loan_produces_both_credits_in_the_first_year(self):
        """The acceptance case, at the 2026 rate the fold projects: 250 x 14% x
        (1 - 16.5%) = 29.225 federal + 250 x 20% = 50.00 Quebec."""
        results = _run()
        assert results[0].consumer_loan_interest == pytest.approx(INTEREST)
        assert results[0].student_loan_credit_applied == pytest.approx(
            INTEREST * 0.14 * (1.0 - 0.165) + INTEREST * 0.20)

    def test_a_loan_that_does_not_qualify_produces_nothing(self):
        """Only interest on a qualifying government loan earns the credits.
        The same loan, same interest, declared non-qualifying: $0 -- and the
        interest still reaches debt service, so this is a real False rather
        than a loan the engine dropped."""
        results = _run(qualifying=False)
        assert results[0].consumer_loan_interest == pytest.approx(INTEREST)
        assert results[0].student_loan_credit_applied == 0.0

    def test_a_consumer_loan_of_another_kind_is_ignored_by_the_credit(self):
        """A car loan sits in the same `consumer_loans` list. It has no
        `qualifying_government_loan` at all (the schema forbids it there), so the
        rule must skip it by KIND rather than reading a missing key."""
        cfg = _cfg()
        cfg['consumer_loans'].append({
            'id': 'car_a', 'kind': 'car_loan', 'owner': 'p1',
            'balance': 20_000.0, 'rate': 0.04, 'payment_monthly': 400.0,
            'amortization_years': 5,
        })
        results = _run_from(cfg)
        assert results[0].consumer_loan_interest > INTEREST  # both loans
        assert results[0].student_loan_credit_applied == pytest.approx(
            INTEREST * 0.14 * (1.0 - 0.165) + INTEREST * 0.20), (
            "the car loan's interest must NOT earn the student-loan credit")

    def test_no_loan_is_a_strict_no_op(self):
        results = _run(with_loan=False)
        assert results[0].student_loan_credit_applied == 0.0
        assert results[0].consumer_loan_interest == 0.0

    def test_a_non_quebec_resident_gets_the_federal_credit_only(self):
        """Ontario: 250 x 14% = 35.00, and no provincial credit (DP#32: never
        credit a provincial portion at a guessed rate)."""
        results = _run(province='on')
        assert results[0].student_loan_credit_applied == pytest.approx(
            INTEREST * 0.14)

    def test_the_credit_is_not_a_refund_and_tracks_the_years_own_rate(self):
        """The rate is year-versioned, so the credit is a function of the year,
        not a constant: the interest declines as the loan amortizes AND the
        federal rate is the year's own."""
        results = _run(projection_years=4)
        assert results[0].student_loan_credit_applied > \
            results[1].student_loan_credit_applied > 0.0


class TestTheCreditIsRealCash:
    def test_the_credit_raises_the_after_tax_income_the_identity_sees(self):
        """The credit lowers tax, so the household keeps more cash: the
        cash-flow identity's after-tax figure must be higher by exactly the
        credit. This is the assertion that fails if the rule computes a number
        nobody consumes."""
        with_credit = _run()
        without = _run(qualifying=False)
        delta = (with_credit[0].after_tax_income - without[0].after_tax_income)
        assert delta == pytest.approx(with_credit[0].student_loan_credit_applied)


class TestTheTwoFoldPathsAgree:
    def test_monthly_matches_yearly_on_the_credit(self):
        yearly = _run(time_step='yearly')
        monthly = _run(time_step='monthly')
        assert [r.student_loan_credit_applied for r in monthly] == \
            [r.student_loan_credit_applied for r in yearly]


class TestTheContractPath:
    """The document side: the flag must survive validation and reach the engine,
    and its absence must be refused rather than assumed."""

    @staticmethod
    def _doc(qualifying=True, declare=True):
        import copy
        from test_input_contract import _load_example
        doc = copy.deepcopy(_load_example())
        liab = {
            "id": "student_a", "owner": "p1", "kind": "student_loan",
            "rate_type": "fixed", "rate": RATE, "collateral": None,
            "balance": {"amount": BALANCE, "as_of": "2026-06-30"},
            "amortization": {"years": 10, "payment_monthly": 100},
        }
        if declare:
            liab["qualifying_government_loan"] = qualifying
        doc["liabilities"] = [liab]
        return doc

    def test_the_flag_reaches_the_engine(self):
        import contract_schema
        import input_contract as ic
        from test_input_contract import _two_generation_subset
        for qualifying in (True, False):
            doc = _two_generation_subset(self._doc(qualifying=qualifying))
            contract_schema.validate_contract(doc)
            internal = ic.to_internal_config(doc)
            loan = next(l for l in internal['consumer_loans']
                        if l['id'] == 'student_a')
            assert loan['qualifying_government_loan'] is qualifying, (
                "the credit is claimed on the owner's return and only on a "
                "qualifying loan, so BOTH facts must reach the engine")
            assert loan['owner'] == 'p1'

    def test_an_absent_flag_is_refused_not_assumed(self):
        import contract_schema
        import contract_errors
        from test_input_contract import _two_generation_subset
        doc = _two_generation_subset(self._doc(declare=False))
        with pytest.raises(contract_errors.ContractValidationError):
            contract_schema.validate_contract(doc)

    def test_the_mapper_refuses_a_hand_built_loan_with_no_flag(self):
        """A config that bypasses the schema must be refused loudly too, not
        defaulted to qualifying (a fabricated credit) or non-qualifying (erasing
        a real one)."""
        import contract_errors
        import contract_liabilities
        doc = {"liabilities": [{
            "id": "student_a", "owner": "p1", "kind": "student_loan",
            "rate": RATE, "collateral": None,
            "balance": {"amount": BALANCE, "as_of": "2026-06-30"},
            "amortization": {"years": 10, "payment_monthly": 100},
        }]}
        with pytest.raises(contract_errors.ContractAdaptationError) as exc:
            contract_liabilities.map_consumer_loans(doc)
        assert 'qualifying_government_loan' in str(exc.value)


class TestAbsenceIsInert:
    def test_the_golden_invariant_is_unmoved(self):
        """The golden household declares no student loan, so no branch here is
        reachable for it."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tests'))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)
