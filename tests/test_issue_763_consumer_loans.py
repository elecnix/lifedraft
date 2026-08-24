#!/usr/bin/env python3
"""Issue #763: non-mortgage liabilities are silently dropped.

``schema/input_schema.json``'s ``$defs.liability_kind`` accepts seven kinds
(mortgage, heloc, line_of_credit, car_loan, student_loan, personal_loan,
intergenerational_loan), but ``input_contract.to_internal_config`` only mapped
the first three. A ``car_loan`` was parsed, schema-validated, accepted -- then
silently dropped before the engine saw it: its balance, rate and monthly
payment never reached debt service, the solvency identity (#679) or the
runway metric (#758). That is THE defect class this repo exists to kill
(AGENTS.md: "the engine silently substitutes zero").

These tests assert the fix end-to-end on the real engine (DP#4/DP#15:
fabricated round numbers, role-based names -- no real household's figures):
  1. A car_loan's payment APPEARS in the solvency debt-service term and the
     reserve/runway sizing (the test that would have caught the drop).
  2. Its balance amortizes to 0 at the declared payoff date and reaches
     total_debt (the balance sheet sees it, not a picture with it erased).
  3. student_loan and personal_loan reach the engine too -- not just car_loan.
  4. Deductibility defaults to NON-deductible (#656 guard): a personal-use
     car_loan's interest is never deducted, and a declared investment_portion
     > 0 is REFUSED LOUDLY rather than silently coerced either way.
  5. intergenerational_loan is REFUSED LOUDLY (#703 lender field not modelled)
     -- never silently dropped (DP#32: accept-and-ignore is not a third state).
  6. A consumer loan secured against real estate is refused (the engine models
     consumer loans as unsecured; a secured one belongs in the charge, #664).
"""
import copy
import json
import logging
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import golden_household_config, _run as _run_golden
import contract_errors
import contract_schema


# ============================================================================
# Fixture helpers (DP#4/DP#15: fabricated, round numbers, role-based names)
# ============================================================================

def _closed_end_liability(kind, *, balance, rate, payment_monthly, years,
                          owner="p1", lid=None, collateral=None,
                          deductibility=None):
    """A minimal schema-valid closed-end consumer liability (fabricated)."""
    liab = {
        "id": lid or f"{kind}_test",
        "owner": owner,
        "kind": kind,
        "balance": {"amount": balance, "as_of": "2026-01-01"},
        "rate": rate,
        "rate_type": "fixed",
        "collateral": collateral,
        "amortization": {"years": years, "payment_monthly": payment_monthly},
    }
    if deductibility is not None:
        liab["deductibility"] = deductibility
    return liab


def _load_example_doc():
    with open(contract_schema.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _doc_with_liabilities(*liabilities, keep_mortgage=True):
    """A two-generation example document whose liabilities are exactly the
    given ones (plus the example's mortgage when keep_mortgage=True), so the
    run has a property + mortgage to service alongside the consumer debt."""
    doc = _load_example_doc()
    new = []
    if keep_mortgage:
        for l in doc["liabilities"]:
            if l["kind"] == "mortgage":
                new.append(l)
                break
    new.extend(liabilities)
    doc["liabilities"] = new
    return doc


def _run(doc, years=None):
    """Map -> SimulationConfig -> run the engine; return the YearResult list."""
    logging.disable(logging.WARNING)
    try:
        cfg = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(cfg)
        if years is not None:
            sim_cfg = SimulationConfig(**{**sim_cfg.__dict__,
                                          "projection_years": years})
        return FamilySimulation(sim_cfg).run()
    finally:
        logging.disable(logging.NOTSET)


def _config_from(doc):
    logging.disable(logging.WARNING)
    try:
        return SimulationConfig.from_dict(ic.to_internal_config(doc))
    finally:
        logging.disable(logging.NOTSET)


# ============================================================================
# 1 + 2. A car_loan reaches debt service, solvency, runway, and the balance sheet
# ============================================================================

class TestCarLoanReachesTheEngine(unittest.TestCase):
    """The reproduction as a regression guard: a car_loan's $700/mo payment
    MUST appear in debt service / solvency / runway, and its balance MUST
    amortize to 0 and reach total_debt."""

    def test_car_loan_payment_appears_in_debt_service(self):
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)
        doc = _doc_with_liabilities(car)
        results = _run(doc)

        mortgage_payment = results[0].mortgage_payment
        car_payment_annual = 700 * 12
        # The solvency identity's debt-service term is the mortgage payment
        # PLUS the car payment -- before #763 it was the mortgage payment alone.
        self.assertAlmostEqual(
            results[0].debt_service, mortgage_payment + car_payment_annual,
            places=2,
            msg="car_loan payment missing from debt service (#763): "
                f"expected mortgage {mortgage_payment:.2f} + car "
                f"{car_payment_annual} = {mortgage_payment+car_payment_annual:.2f}, "
                f"got {results[0].debt_service:.2f}")

    def test_car_loan_balance_amortizes_to_zero_and_reaches_total_debt(self):
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)
        doc = _doc_with_liabilities(car)
        results = _run(doc, years=7)

        # The balance declines monotonically and reaches 0 by the payoff date
        # (year 5), then stays 0 -- it does not linger, and it does not vanish
        # at year 0 (the silent-drop symptom).
        balances = [r.consumer_loan_balance for r in results]
        self.assertGreater(balances[0], 0, "car_loan balance never reached the engine")
        self.assertEqual(balances[5], 0.0, f"car_loan not paid off by year 5: {balances}")
        for i in range(1, 5):
            self.assertLess(balances[i], balances[i - 1],
                            f"car_loan balance did not decline at year {i}: {balances}")
        # The balance is folded into total_debt every year it is outstanding.
        mortgage_balance_y0 = results[0].mortgage_balance
        self.assertAlmostEqual(
            results[0].total_debt, mortgage_balance_y0 + balances[0], places=2,
            msg="car_loan balance not folded into total_debt")

    def test_car_loan_payment_reaches_the_runway_reserve_target(self):
        """#758: the emergency-reserve target is sized against BOTH the
        mortgage payment AND the consumer-loan payment -- a car payment the
        household must make is exactly the outflow a reserve exists to bridge."""
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)
        with_car = _doc_with_liabilities(car)
        without_car = _doc_with_liabilities()  # mortgage only

        with_car_results = _run(with_car)
        without_car_results = _run(without_car)
        # The reserve target grows by the car payment's monthly contribution
        # (reserve_target = target_months * (living_costs + debt_service) / 12),
        # so a declared car loan MUST raise the target above the no-car run.
        self.assertGreater(
            with_car_results[0].emergency_reserve_target,
            without_car_results[0].emergency_reserve_target,
            "car_loan payment did not raise the reserve/runway target (#758)")

    def test_over_amortizing_payment_is_clamped_and_closes_the_loan_early(self):
        """A declared payment far larger than the balance must NOT drive the
        balance negative: the rule clamps the year's payment to
        ``min(annual_payment, balance + interest)`` and floors the new balance
        at 0 (apply_consumer_loans, the non-final-year branch). The year after,
        the already-zero balance takes the ``bal <= 0`` off-books path and the
        loan pays nothing. Without the clamp, an aggressive payment would
        overshoot into a negative (i.e. fictitious) balance."""
        # Year 0 (not final, term 5): annual payment 1,200 < balance 2,000 +
        # interest, so min() picks the annual payment -> balance falls to ~850.
        # Year 1 (still not final): the remaining balance + interest (~893) is
        # now LESS than the annual payment, so min() picks balance+interest ->
        # the clamp fires in a non-final year and floors the balance at 0.
        # Year 2: the zero balance takes the ``bal <= 0`` off-books branch.
        loan = _closed_end_liability(
            "personal_loan", balance=2_000, rate=0.05, payment_monthly=100, years=5)
        results = _run(_doc_with_liabilities(loan), years=3)
        balances = [r.consumer_loan_balance for r in results]

        self.assertGreater(balances[0], 0.0,
                           "personal_loan balance never reached the engine")
        self.assertEqual(balances[1], 0.0,
                         f"over-amortizing loan not closed after year 0: {balances}")
        for b in balances:
            self.assertGreaterEqual(
                b, 0.0, f"balance went negative -- clamp failed: {balances}")


# ============================================================================
# 3. student_loan and personal_loan reach the engine too
# ============================================================================

class TestEveryConsumedKindReachesTheEngine(unittest.TestCase):

    def test_student_loan_payment_reaches_debt_service(self):
        loan = _closed_end_liability(
            "student_loan", balance=25_000, rate=0.05, payment_monthly=300, years=10)
        results = _run(_doc_with_liabilities(loan))
        self.assertAlmostEqual(
            results[0].debt_service, results[0].mortgage_payment + 300 * 12, places=2)
        self.assertGreater(results[0].consumer_loan_balance, 0)

    def test_personal_loan_payment_reaches_debt_service(self):
        loan = _closed_end_liability(
            "personal_loan", balance=10_000, rate=0.08, payment_monthly=500, years=2)
        results = _run(_doc_with_liabilities(loan))
        self.assertAlmostEqual(
            results[0].debt_service, results[0].mortgage_payment + 500 * 12, places=2)

    def test_multiple_consumer_loans_all_reach_debt_service(self):
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5,
            lid="car_a")
        student = _closed_end_liability(
            "student_loan", balance=25_000, rate=0.05, payment_monthly=300, years=10,
            owner="p2", lid="student_b")
        results = _run(_doc_with_liabilities(car, student))
        expected = results[0].mortgage_payment + (700 + 300) * 12
        self.assertAlmostEqual(results[0].debt_service, expected, places=2)


# ============================================================================
# 4. Deductibility defaults to NON-deductible (#656 guard)
# ============================================================================

class TestDeductibilityDefaultsNonDeductible(unittest.TestCase):

    def test_personal_use_car_loan_interest_is_not_deducted(self):
        """A car_loan with no deductibility (the common, personal-use case)
        defaults to non-deductible: its interest is part of the after-tax
        debt-service payment, never a tax deduction (#656 -- the default must
        not be the favourable 'deductible' direction)."""
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)
        cfg = _config_from(_doc_with_liabilities(car))
        # The consumer loan is mapped with no deductibility field at all --
        # the engine has no path that deducts consumer-loan interest, and the
        # config carries no investment_portion to default it deductible from.
        self.assertEqual(cfg.consumer_loans[0]["kind"], "car_loan")
        self.assertNotIn("investment_portion", cfg.consumer_loans[0])
        # Year-0 interest is reported as a non-deductible cost (part of the
        # payment), not as a tax saving anywhere on the result.
        results = _run(_doc_with_liabilities(car))
        self.assertGreater(results[0].consumer_loan_interest, 0)
        # readvance_tax_savings is the engine's deductible-interest ledger
        # (HELOC/Smith); a non-deductible consumer loan must not move it.
        self.assertEqual(results[0].readvance_tax_savings, 0.0)

    def test_declared_deductible_consumer_loan_is_refused_loudly(self):
        """#656/#703: a consumer loan declaring investment_portion > 0
        (asserting deductible interest) is REFUSED at load -- never silently
        coerced to deductible (the #656 direction) nor silently dropped to
        non-deductible (the DP#32 drop-the-declaration direction)."""
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5,
            deductibility={"investment_portion": 0.5, "personal_portion": 0.5})
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_liabilities(car))
        self.assertIn("investment_portion", str(cm.exception))
        self.assertIn("#703", str(cm.exception))


# ============================================================================
# 5. intergenerational_loan is refused loudly (#703), never silently dropped
# ============================================================================

class TestIntergenerationalLoanRefused(unittest.TestCase):

    def test_intergenerational_loan_is_refused_not_dropped(self):
        """DP#32: a liability kind the schema accepts but the engine cannot
        yet consume must FAIL LOUDLY, not be silently dropped. The lender-
        identity field (#703) is not modelled, so the contract is refused."""
        loan = _closed_end_liability(
            "intergenerational_loan", balance=50_000, rate=0.02,
            payment_monthly=400, years=15)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_liabilities(loan))
        self.assertIn("intergenerational_loan", str(cm.exception))
        self.assertIn("#703", str(cm.exception))


# ============================================================================
# 6. A secured consumer loan is refused (modeled unsecured; belongs in the charge)
# ============================================================================

class TestSecuredConsumerLoanRefused(unittest.TestCase):

    def test_consumer_loan_secured_against_property_is_refused(self):
        doc = _doc_with_liabilities()
        principal_id = next(p["id"] for p in doc["properties"]
                            if p["kind"] == "principal")
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5,
            collateral=principal_id)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_liabilities(car))
        self.assertIn("collateral", str(cm.exception))


# ============================================================================
# 7. Round-trip (DP#24) + the state/config mismatch guard
# ============================================================================

class TestRoundTripAndGuards(unittest.TestCase):

    def test_consumer_loans_round_trip_through_to_dict(self):
        """DP#24: a config built from a contract with a car_loan must export
        its consumer_loans via to_dict and reload via from_dict unchanged --
        so a saved-and-rerun scenario keeps the car payment, not a picture
        with it erased."""
        car = _closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)
        cfg = _config_from(_doc_with_liabilities(car))
        self.assertTrue(cfg.consumer_loans)
        exported = cfg.to_dict()
        self.assertIn("consumer_loans", exported, "to_dict dropped consumer_loans")
        reloaded = SimulationConfig.from_dict(exported)
        self.assertEqual(reloaded.consumer_loans, cfg.consumer_loans)

    def test_mismatched_state_config_raises_loudly(self):
        """The apply_consumer_loans rule refuses a SimState whose
        consumer_loan_balances length disagrees with config.consumer_loans --
        a programming error in the simulation wiring, not a silent
        truncation (DP#32)."""
        from simulation_state import SimState, _default_canada_state, simulate_year_pure
        cfg = _config_from(_doc_with_liabilities(_closed_end_liability(
            "car_loan", balance=40_000, rate=0.03, payment_monthly=700, years=5)))
        # Two consumer loans on the config, but the state carries one balance
        # -- a length mismatch no legitimate flow produces (SimState.initial
        # seeds them parallel), but a future bug could.
        cfg2 = SimulationConfig(**{**cfg.__dict__, "consumer_loans": [
            cfg.consumer_loans[0],
            {**cfg.consumer_loans[0], "id": "car_loan_g2"},
        ]})
        mismatched = SimState(
            consumer_loan_balances=[40_000],
            jurisdiction_state={'canada': _default_canada_state()},
        )
        with self.assertRaises(ValueError) as cm:
            simulate_year_pure(
                state=mismatched, year=0,
                allocations={'_primary_income': 130_000, '_annual_savings': 0},
                config=cfg2, investment_return=0.06, primary_marginal_rate=0.40)
        self.assertIn("consumer_loans", str(cm.exception))


class TestRetirementDrawdownDrawsLiraAndFhsa(unittest.TestCase):
    """Cover the LIRA and FHSA balance-delta branches of the
    ``@rule('retirement_drawdown')`` apply loop (``simulation_rules.py``:
    ``elif key == 'lira_balance'`` / ``elif key == 'fhsa_balance'``).

    This is pre-existing engine code, not consumer-loan code: it only executes
    when a retirement drawdown ORDER includes ``lira``/``fhsa``, those accounts
    hold a balance, and the net spending target is high enough to exhaust the
    earlier accounts and reach them. No existing test drove a drawdown into
    BOTH a LIRA and an FHSA, so ``plan_drawdown_net`` never emitted those
    balance-delta keys and the two branches never ran. This PR shifted those
    lines, surfacing the gap in the coverage gate; per AGENTS.md ("when the
    gate fires, write a test, don't grow the allowlist") this covers them.

    It is also a real DP#32 property, not a coverage stunt: a drawdown order
    that names ``lira`` and ``fhsa`` on a household holding both must ACTUALLY
    DRAW them once earlier accounts are exhausted -- not silently leave them
    untouched (which would be the engine "substituting zero" for a source it
    was told to use). Fabricated round numbers, role-based names (DP#4/DP#15).
    """

    @staticmethod
    def _results():
        # A golden-shaped couple, but with the accumulation pots kept small
        # (savings rate 0, RRSP contributions 0, tiny opening RRSP/TFSA) so a
        # high retirement spend blows through TFSA/RRSP/LIF and reaches the
        # LIRA (before its statutory conversion to a LIF) and then the FHSA.
        cfg = copy.deepcopy(golden_household_config())
        members = cfg["family"]["members"]
        members[0]["rrsp_balance"] = 30_000
        members[0]["tfsa_balance"] = 5_000
        members[0]["fhsa_balance"] = 100_000
        members[1]["rrsp_balance"] = 20_000
        members[1]["tfsa_balance"] = 5_000
        cfg["portfolio"]["accounts"]["non_reg"]["balance"] = 0
        cfg["savings"]["rate"] = 0.0
        cfg["accounts"]["rrsp_annual_max"] = 0
        # A locked-in retirement account (CRI/LIRA), top-level per #293.
        cfg["lira"] = {
            "balance": 300_000,
            "birth_year": members[0]["birth_year"],
            "reference_rate": 0.06,
            "jurisdiction": "quebec",
        }
        # Drawdown order that reaches the LIRA and FHSA after the others.
        cfg["retirement"]["drawdown_order"] = [
            "tfsa", "non_reg", "rrsp", "lif", "lira", "fhsa",
        ]
        cfg["retirement"]["spending_target"] = 250_000
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _run_golden(cfg)

    def test_lira_is_drawn_down_during_retirement(self):
        """The LIRA balance falls year-over-year in a retirement drawdown year
        (before it converts to a LIF) -- proving the drawdown applied the
        ``lira_balance`` delta rather than leaving the account to grow."""
        rs = self._results()
        drew_from_lira = False
        prev = None
        for r in rs:
            if (prev is not None and prev > 0 and 0 < r.lira_balance < prev
                    and r.drawdown_net_target > 0):
                drew_from_lira = True
                break
            prev = r.lira_balance
        self.assertTrue(
            drew_from_lira,
            "expected a retirement year where the LIRA balance is drawn down "
            "(delta applied) while it still holds a balance; the drawdown "
            "order includes 'lira' and the household holds one")

    def test_fhsa_funds_a_drawdown_after_every_other_source_is_empty(self):
        """A year delivers NET drawdown with ZERO taxable withdrawal while
        RRSP/TFSA/LIRA/LIF/non-reg are all empty -- the only remaining
        (non-taxable) source in the order is the FHSA, so that delivery can
        only have come from applying the ``fhsa_balance`` delta."""
        rs = self._results()
        fhsa_sourced = [
            r for r in rs
            if r.drawdown_net_delivered > 1.0
            and r.drawdown_taxable == 0.0
            and r.total_rrsp == 0.0 and r.total_tfsa == 0.0
            and r.lira_balance == 0.0 and r.lif_balance == 0.0
            and r.non_reg_balance == 0.0
        ]
        self.assertGreater(
            len(fhsa_sourced), 0,
            "expected a year whose net drawdown could only have been funded by "
            "the FHSA (every taxable and other tax-free source already at 0); "
            "if this is empty the fhsa_balance delta branch never ran")


if __name__ == "__main__":
    unittest.main()