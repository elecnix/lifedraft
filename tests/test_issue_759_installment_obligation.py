#!/usr/bin/env python3
"""Issue #759: the contract cannot express a fixed-term, zero-interest
installment obligation.

A household may commit to a medical/dental/education payment plan: an up-front
lump already paid, then N equal monthly payments, then an optional final
balloon, at 0% interest, over a FIXED term. This is neither a mortgage, nor
revolving credit, nor a recurring living cost. It is **non-negotiable** in a
job-loss year (unlike discretionary spending) AND it **ends** (unlike a
perpetual living cost). Before this issue the only place it could go was
``household_budget.annual_living_costs``, which smeared a finite, must-pay plan
into a perpetual, compressible-looking scalar -- the wrong behavior these
tests reproduce first, then guard the fix against (DP#4/DP#15: fabricated round
numbers, role-based names -- no real household's figures).

The fix is a first-class ``installments[]`` block (schema/input_schema.json)
mapped by input_contract into ``SimulationConfig.installments`` and serviced by
``simulation_rules.apply_installments``. Its payment folds into the solvency
identity's debt-service term and the #758 reserve/runway sizing -- the same
NON-COMPRESSIBLE channel as the mortgage + consumer-loan payments -- and STOPS
the year after the final payment date (it is never carried to the horizon).
"""
import copy
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
import input_contract as ic
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import SimState, _default_canada_state

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import _run as _run_golden
from test_golden_trajectory_581 import golden_household_config
import contract_errors
import contract_schema

# ============================================================================
# Fixture helpers (DP#4/DP#15: fabricated, round numbers, role-based names)
# ============================================================================

def _installment_plan(*, plan_id="ortho_plan", owner="p1", description="orthodontic plan",
                     start_date="2026-09-01", monthly_amount=500, number_of_payments=24,
                     final_payment=1000, rate=0, non_discretionary=True):
    """A minimal schema-valid fixed-term installment obligation (fabricated)."""
    plan = {
        "id": plan_id,
        "owner": owner,
        "description": description,
        "start_date": start_date,
        "monthly_amount": monthly_amount,
        "number_of_payments": number_of_payments,
        "rate": rate,
        "non_discretionary": non_discretionary,
    }
    if final_payment is not None:
        plan["final_payment"] = final_payment
    return plan


def _load_example_doc():
    with open(contract_schema.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _doc_with_installments(*plans, keep_mortgage=True):
    """A two-generation example document carrying the given installment
    plans (plus the example's mortgage when keep_mortgage=True), so the run
    has a property + mortgage to service alongside the plan."""
    doc = _load_example_doc()
    if not keep_mortgage:
        doc["liabilities"] = [liab for liab in doc["liabilities"] if liab["kind"] != "mortgage"]
    doc["installments"] = list(plans)
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
# 0. Reproduction: today the plan can only be smeared into annual_living_costs,
#    which is PERPETUAL -- the finite plan never ends. (The fix's installment
#    path ENDS; this test contrasts the two on the SAME dollar amount.)
# ============================================================================

class TestReproductionSmearedIntoLivingCostsIsPerpetual(unittest.TestCase):
    """The wrong behavior the issue opens with: a $500/mo, 24-month plan is a
    FINITE ~$13k obligation. Smeared into ``annual_living_costs`` it becomes a
    PERPETUAL $6,000/year scalar carried to the horizon (through retirement,
    into the estate). The first-class ``installments[]`` block instead ENDS
    the year after the final payment. Same dollars, opposite shape."""

    def test_smeared_plan_lingers_past_its_end_date(self):
        """A 24-month plan ends in 2028. Smeared into annual_living_costs the
        $6,000/year outflow is STILL present in 2030 (year 5) -- the engine
        has no end date for it. This is the defect, measured on the real
        engine's own reported ``living_costs`` field."""
        doc = _load_example_doc()
        base_living = doc["household_budget"]["annual_living_costs"]
        smeared = copy.deepcopy(doc)
        smeared["household_budget"]["annual_living_costs"] = base_living + 6_000
        rs = _run(smeared, years=5)
        # 2030 is year 5 (start_year 2026). The smeared $6,000 is still in the
        # reported working-phase living_costs -- the plan never ended.
        self.assertGreater(
            rs[-1].living_costs, base_living,
            "the smeared plan was NOT carried to the horizon -- the "
            "reproduction of the wrong behavior did not reproduce (the "
            "whole point of issue #759 is that annual_living_costs is "
            "perpetual, so a finite plan smeared into it lingers forever)")

    def test_installment_plan_ends_at_its_final_payment_date(self):
        """The SAME $500/mo, 24-month plan, declared as an ``installments[]``
        block, pays in 2026-2028 and is ZERO in 2030 (year 5) -- it ENDS at
        its declared final payment date, the behavior the fix adds and the
        smear cannot produce."""
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        rs = _run(_doc_with_installments(plan), years=5)
        # 2026 (year 1): 4 payments (Sep-Dec) = 2,000. Active.
        self.assertGreater(rs[0].installment_payment, 0)
        # 2030 (year 5): the plan ended in 2028 -- ZERO, not carried forward.
        self.assertEqual(
            rs[-1].installment_payment, 0.0,
            f"installment plan did not END at its final payment date -- it "
            f"lingered to year {rs[-1].year} ({rs[-1].installment_payment}), "
            f"the exact perpetual-smear defect issue #759 exists to fix")
        # And the installment path did NOT inflate the reported living_costs
        # (it is a separate, non-discretionary outflow, not a living cost).
        self.assertEqual(
            rs[-1].living_costs, _load_example_doc()["household_budget"]["annual_living_costs"],
            "the installment payment leaked into the reported living_costs -- "
            "it must be a separate outflow, not a smear")


# ============================================================================
# 1. The payment appears in debt service while active, and STOPS at the end
# ============================================================================

class TestInstallmentReachesDebtService(unittest.TestCase):
    """The payment MUST appear in the solvency debt-service term while active
    (the test that would have caught a silent drop, mirroring #763's
    car_loan test), and its balance MUST decline to 0 and not linger."""

    def test_payment_appears_in_debt_service(self):
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        rs = _run(_doc_with_installments(plan))
        # 2026: 4 monthly payments = 2,000. The debt-service term is the
        # mortgage + consumer loans + the installment payment -- before #759
        # the installment contribution was missing (the smear does not reach
        # debt_service at all; it reaches living_costs instead).
        rs_no_plan = _run(_doc_with_installments())  # mortgage + consumer loans only
        self.assertAlmostEqual(
            rs[0].debt_service, rs_no_plan[0].debt_service + rs[0].installment_payment,
            places=2,
            msg="installment payment missing from debt service (#759)")

    def test_balance_amortizes_to_zero_and_does_not_linger(self):
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        rs = _run(_doc_with_installments(plan), years=6)
        balances = [r.installment_balance for r in rs]
        # 24*500 + 1000 = 13,000 opening; the reported balance is END-of-year
        # (the new balance the fold carries forward), so year 1 (2026) ends at
        # 13,000 - 4*500 = 11,000. It declines monotonically to 0 by the
        # final-payment year (2028, year 3), then stays 0 -- no lingering.
        self.assertAlmostEqual(balances[0], 11_000.0, places=2,
                               msg="installment balance never reached the engine")
        for i in range(1, 3):
            self.assertLess(balances[i], balances[i - 1],
                            f"installment balance did not decline at year {i}: {balances}")
        self.assertEqual(balances[2], 0.0,
                         f"installment balance not 0 by the final-payment year (2028): {balances}")
        for b in balances[2:]:
            self.assertEqual(b, 0.0, f"installment balance lingered past end: {balances}")

    def test_payment_drops_to_zero_after_final_payment(self):
        """A plan ending in 2028 pays nothing in 2029 or later -- the
        outflow TERMINATES on its end date, it is not carried to the
        horizon (the headline acceptance criterion)."""
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        rs = _run(_doc_with_installments(plan), years=6)
        # 2028 (year 3) is the final-payment year (8 monthly + the balloon).
        self.assertGreater(rs[2].installment_payment, 0)
        # 2029 (year 4) onward: zero.
        for r in rs[3:]:
            self.assertEqual(r.installment_payment, 0.0,
                             f"installment payment did not drop to 0 in {r.year}")

    def test_final_payment_balloon_lands_in_the_final_year(self):
        """The optional final balloon is paid in the SAME year as the last
        monthly payment, not spread, not dropped, not deferred."""
        # as_of is 2026-07-12, so start_date must be on/after it: 2026-08-01.
        # 12 monthly payments from 2026-08 end 2027-07; the balloon lands in
        # 2027 (year 2), the same year as the last monthly payment.
        plan = _installment_plan(monthly_amount=100, number_of_payments=12,
                                 final_payment=5_000, start_date="2026-08-01")
        rs = _run(_doc_with_installments(plan), years=3)
        # Opening = 12*100 + 5000 = 6,200. Year 1 (2026): 5 payments (Aug-Dec)
        # = 500; balance 5,700. Year 2 (2027): 7 monthly + 5,000 balloon =
        # 5,700; balance 0.
        self.assertAlmostEqual(rs[0].installment_payment, 500.0, places=2)
        self.assertAlmostEqual(rs[0].installment_balance, 5_700.0, places=2)
        self.assertAlmostEqual(rs[1].installment_payment, 5_700.0, places=2)
        self.assertEqual(rs[1].installment_balance, 0.0)
        self.assertEqual(rs[2].installment_payment, 0.0)

    def test_plan_with_no_final_payment(self):
        """An omitted final_payment = no balloon (a structural absence, not a
        silent zero). The plan pays exactly N * monthly and ends."""
        # start_date 2026-08-01 (>= as_of): 12 payments end 2027-07.
        plan = _installment_plan(final_payment=None, monthly_amount=400,
                                 number_of_payments=12, start_date="2026-08-01")
        rs = _run(_doc_with_installments(plan), years=3)
        # Opening 4,800. Year 1 (2026): 5*400 = 2,000; balance 2,800. Year 2
        # (2027): 7*400 = 2,800; balance 0.
        self.assertAlmostEqual(rs[0].installment_payment, 2_000.0, places=2)
        self.assertAlmostEqual(rs[0].installment_balance, 2_800.0, places=2)
        self.assertAlmostEqual(rs[1].installment_payment, 2_800.0, places=2)
        self.assertEqual(rs[1].installment_balance, 0.0)
        self.assertEqual(rs[2].installment_payment, 0.0)

    def test_future_start_date_holds_balance_until_active(self):
        """A plan whose start_date is in a LATER year pays nothing and holds
        its full balance until the start year -- the start_date is honored,
        not silently collapsed to year 0 (contrast consumer_loans, which
        amortize from year 0 with no start-date field)."""
        plan = _installment_plan(monthly_amount=500, number_of_payments=12,
                                 final_payment=0, start_date="2028-01-01")
        # final_payment=0 is a real declared zero (the schema's money $ref
        # allows 0); pass it explicitly rather than omitting.
        rs = _run(_doc_with_installments(plan), years=4)
        # 2026 + 2027 (years 1, 2): not yet active -- 0 payment, full balance.
        self.assertEqual(rs[0].installment_payment, 0.0)
        self.assertAlmostEqual(rs[0].installment_balance, 6_000.0, places=2)
        self.assertEqual(rs[1].installment_payment, 0.0)
        self.assertAlmostEqual(rs[1].installment_balance, 6_000.0, places=2)
        # 2028 (year 3): active -- 12 * 500 = 6,000, balance -> 0.
        self.assertAlmostEqual(rs[2].installment_payment, 6_000.0, places=2)
        self.assertEqual(rs[2].installment_balance, 0.0)
        self.assertEqual(rs[3].installment_payment, 0.0)


# ============================================================================
# 2. Counted in the emergency-reserve / runway sizing (#758)
# ============================================================================

class TestInstallmentReachesReserveRunwaySizing(unittest.TestCase):
    """#758: the reserve/runway target is sized against the household's
    essential outflows. A contractual payment plan the household must make is
    exactly the outflow a reserve exists to bridge -- declaring it MUST raise
    the target above the no-plan run (mirroring #763's car-loan reserve test)."""

    def test_installment_payment_raises_the_reserve_target(self):
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        with_plan = _run(_doc_with_installments(plan))
        without_plan = _run(_doc_with_installments())
        self.assertGreater(
            with_plan[0].emergency_reserve_target,
            without_plan[0].emergency_reserve_target,
            "installment payment did not raise the reserve/runway target (#758)")

    def test_installment_balance_is_not_in_total_debt(self):
        """An installment plan is a committed payment schedule, NOT a callable
        borrowing against the estate -- its remaining balance is reported for
        transparency but must NOT be folded into total_debt (contrast a
        consumer loan, which IS real debt and IS in total_debt)."""
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        with_plan = _run(_doc_with_installments(plan))
        without_plan = _run(_doc_with_installments())
        # The installment balance ($13,000 in year 0) is NOT in total_debt --
        # adding the plan must not raise total_debt by the plan's balance.
        self.assertAlmostEqual(
            with_plan[0].total_debt, without_plan[0].total_debt, places=2,
            msg="installment balance leaked into total_debt -- it is not a "
                "callable borrowing (issue #759: not a liabilities[] kind)")
        # ...yet the balance IS reported on the result for transparency.
        self.assertGreater(with_plan[0].installment_balance, 0)


# ============================================================================
# 3. Non-negotiable under stress (does NOT compress like discretionary spend)
# ============================================================================

class TestNonDiscretionaryUnderStress(unittest.TestCase):
    """The installment payment lands in the solvency debt-service term -- the
    SAME non-compressible channel as the mortgage + consumer-loan payments.
    An income-shock year cannot cut it (contrast #761's discretionary split,
    which compresses ``annual_living_costs``). Measured directly: the payment
    is part of ``debt_service``, never part of the compressible ``living_costs``."""

    def test_payment_is_in_debt_service_not_living_costs(self):
        plan = _installment_plan(monthly_amount=500, number_of_payments=24,
                                 final_payment=1000, start_date="2026-09-01")
        base = _load_example_doc()
        rs = _run(_doc_with_installments(plan))
        # The installment payment is counted in debt_service (non-compressible)
        # and the reported living_costs is unchanged from the base contract --
        # i.e. the plan did NOT inflate the discretionary/compressible scalar.
        self.assertEqual(rs[0].living_costs, base["household_budget"]["annual_living_costs"])
        self.assertGreater(rs[0].installment_payment, 0)
        self.assertGreater(rs[0].debt_service, 0)
        # The debt-service term includes the installment payment (the
        # non-discretionary channel); living_costs does not.
        self.assertEqual(
            rs[0].living_costs, base["household_budget"]["annual_living_costs"],
            "installment payment leaked into living_costs -- it must stay in "
            "the non-compressible debt-service term (#759 vs #761)")


# ============================================================================
# 4. DP#32: absence is a no-op (the golden invariant must not move)
# ============================================================================

class TestAbsenceIsNoOp(unittest.TestCase):
    """A contract with no ``installments[]`` block must run identically to
    before -- a new OPTIONAL feature is a no-op on a contract that does not
    use it (DP#32). The golden household declares no installment plan, so its
    terminal total_assets must be the committed invariant."""

    GOLDEN = 9709753.139463063

    def test_golden_invariant_unchanged(self):
        """Method: ran the golden_household_config() fixture through _run and
        read the terminal YearResult.total_assets. The golden household
        declares no installments[], so the new code path is never engaged and
        the invariant must not move (it is computed, not stored -- AGENTS.md)."""
        rs = _run_golden(golden_household_config())
        self.assertEqual(
            repr(rs[-1].total_assets), repr(self.GOLDEN),
            "the golden invariant MOVED -- a new optional installments[] block "
            "must be a no-op on a contract that does not declare one (DP#32). "
            "If this moved, the wiring engaged when it should not have.")

    def test_example_without_installments_runs_unchanged(self):
        """The shipped example (no installments[]) maps to a config with an
        empty installments list and runs with installment_payment == 0 every
        year -- the block's absence is a hard zero, never a fabricated plan."""
        cfg = _config_from(_load_example_doc())
        self.assertEqual(cfg.installments, [])
        rs = _run(_load_example_doc())
        for r in rs:
            self.assertEqual(r.installment_payment, 0.0)
            self.assertEqual(r.installment_balance, 0.0)


# ============================================================================
# 5. DP#32: a partial / unsupported declaration FAILS LOUDLY at load
# ============================================================================

class TestPartialOrUnsupportedDeclarationRefused(unittest.TestCase):
    """The contract boundary refuses what it cannot honor, in either direction
    -- never silently coerces to the supported value (the founding DP#32
    defect). A stated rate, a discretionary flag, or a pre-snapshot start date
    each assert something the engine cannot model and are REFUSED."""

    def test_nonzero_rate_is_refused(self):
        """A stated-rate plan's interest pricing is out of scope (#759); a
        non-zero rate is refused, not silently dropped to 0 nor honored."""
        plan = _installment_plan(rate=0.05)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_installments(plan))
        self.assertIn("rate", str(cm.exception))
        self.assertIn("#759", str(cm.exception))

    def test_discretionary_flag_is_refused(self):
        """An installment plan is must-pay by construction; a false
        non_discretionary is refused, not silently treated as compressible."""
        plan = _installment_plan(non_discretionary=False)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_installments(plan))
        self.assertIn("non_discretionary", str(cm.exception))

    def test_start_date_before_as_of_is_refused(self):
        """A plan that started before the snapshot has already-paid installments
        the year-stepped simulation cannot re-price; refused, not silently
        dropped to the post-as_of portion."""
        plan = _installment_plan(start_date="2026-01-01")  # as_of is 2026-07-12
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _config_from(_doc_with_installments(plan))
        self.assertIn("start_date", str(cm.exception))
        self.assertIn("as_of", str(cm.exception))

    def test_missing_required_field_is_a_schema_error(self):
        """A plan missing a required field (e.g. number_of_payments) is rejected
        by the schema at the one loading boundary -- never silently defaulted
        (DP#32: a partial declaration fails loudly, never defaults the missing
        piece to zero / to perpetual)."""
        plan = _installment_plan()
        del plan["number_of_payments"]
        with self.assertRaises(contract_errors.ContractValidationError):
            _config_from(_doc_with_installments(plan))


# ============================================================================
# 6. Round-trip (DP#24) + the state/config mismatch guard
# ============================================================================

class TestRoundTripAndGuards(unittest.TestCase):

    def test_installments_round_trip_through_to_dict(self):
        """DP#24: a config built from a contract with an installment plan must
        export its installments via to_dict and reload via from_dict unchanged
        -- so a saved-and-rerun scenario keeps the payment, not a picture with
        it erased."""
        plan = _installment_plan()
        cfg = _config_from(_doc_with_installments(plan))
        self.assertTrue(cfg.installments)
        exported = cfg.to_dict()
        self.assertIn("installments", exported, "to_dict dropped installments")
        reloaded = SimulationConfig.from_dict(exported)
        self.assertEqual(reloaded.installments, cfg.installments)

    def test_absent_installments_do_not_round_trip_as_a_fabricated_block(self):
        """A contract with no installments round-trips to an EMPTY list, never
        to a fabricated block (DP#24/DP#32 -- absence stays absent)."""
        cfg = _config_from(_load_example_doc())
        exported = cfg.to_dict()
        self.assertNotIn("installments", exported)
        self.assertEqual(cfg.installments, [])

    def test_mismatched_state_config_raises_loudly(self):
        """The apply_installments rule refuses a SimState whose
        installment_balances length disagrees with config.installments -- a
        programming error in the simulation wiring, not a silent truncation
        (DP#32, mirroring apply_consumer_loans' guard)."""
        from simulation_state import SimState, _default_canada_state, simulate_year_pure
        cfg = _config_from(_doc_with_installments(_installment_plan()))
        # Two plans on the config, but the state carries one balance -- a
        # length mismatch no legitimate flow produces (SimState.initial seeds
        # them parallel), but a future bug could.
        cfg2 = SimulationConfig(**{**cfg.__dict__, "installments": [
            cfg.installments[0],
            {**cfg.installments[0], "id": "ortho_plan_g2"},
        ]})
        mismatched = SimState(
            # Keep consumer_loan_balances consistent with the example's car_loan
            # so apply_consumer_loans does not raise first (this test isolates
            # the installments mismatch, mirroring the consumer-loan test's
            # single-rule isolation).
            consumer_loan_balances=[cfg.consumer_loans[0]['balance']] if cfg.consumer_loans else [],
            installment_balances=[13_000],
            jurisdiction_state={'canada': _default_canada_state()},
        )
        with self.assertRaises(ValueError) as cm:
            simulate_year_pure(
                state=mismatched, year=0,
                allocations={'_primary_income': 130_000, '_annual_savings': 0},
                config=cfg2, investment_return=0.06, primary_marginal_rate=0.40)
        self.assertIn("installments", str(cm.exception))

    def test_simstate_deepcopy_and_fork_carry_installment_balances(self):
        """The SimState copy/fork paths must carry installment_balances the
        same way they carry consumer_loan_balances -- an optimizer fork that
        dropped the remaining-payment balance would silently restart a
        finished plan (the DP#18/DP#32 money-flow defect). Covers the
        __deepcopy__ and fork installment lines the coverage gate would
        otherwise flag as new uncovered code."""
        s = SimState(installment_balances=[13_000, 4_800],
                     consumer_loan_balances=[2_000],
                     jurisdiction_state={'canada': _default_canada_state()})
        dc = copy.deepcopy(s)
        self.assertEqual(dc.installment_balances, [13_000, 4_800])
        dc.installment_balances[0] = 0  # mutating the copy must not touch the original
        self.assertEqual(s.installment_balances[0], 13_000)
        f = SimState.fork(s)
        self.assertEqual(f.installment_balances, [13_000, 4_800])
        f.installment_balances[0] = 99
        self.assertEqual(s.installment_balances[0], 13_000)


if __name__ == "__main__":
    unittest.main()
