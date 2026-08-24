#!/usr/bin/env python3
"""Issue #142: ITA s.20(1)(e) -- a SEPARATELY-CHARGED investment management /
counsel fee on a non-registered account is now a first-class fact with both of
its real-world effects, instead of an orphaned AMT half-add-back:

  - CASH: every declared ``deductible_management_fee_annual`` becomes one
    negative dated cash-flow leg per projection year (the #138/#139 channel),
    so the fee leaves the household -- it is not a phantom deduction;
  - TAX: the fee reduces its OWNER's taxable income bracket-aware while they
    work (the same income-reduction trace as the RRSP/Smith/rental
    deductions, #813/#1033), and once they are retired it comes off the
    drawdown bases at the source so plan_drawdown_net's bounded OAS-clawback
    fixpoint sees the reduced net income. Exactly one mechanism fires per
    phase (#1033 gate pattern) -- no dollar is deducted twice;
  - AMT: rules_amt nets the fee out of the AMTI base AND passes it as
    ``carrying_charges``, so s.127.52(1)(j)(ii)'s half-add-back finally
    references a deduction the ordinary tax path actually granted.

The EMBEDDED MER (#691/#136) is a different spelling of a different fact: it
drags growth only. Both declared => BOTH apply (a discretionary mandate bills
on top of the funds' own MERs); neither double-charges the other.

DP#32 both ways: a fee on a registered account is REFUSED loudly (s.20(1)(e)
covers non-registered investments only), and a household declaring no fee
maps and runs BYTE-IDENTICALLY to before (golden terminal total_assets
untouched).

DP#4/DP#15: every figure below is fabricated and round ($500,000 portfolio,
$3,000/yr fee); every name is role-based. No real manager, fee, or person.
"""
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
import contract_schema
import contract_errors
from contract_accounts import map_management_fee_legs
from contract_schema import validate_contract
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from tax_calculator import tax_on_income
from tax_data import default_tax_provider
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import (
    golden_household_config, _run as _run_golden,
)

TERMINAL_TOTAL_ASSETS = 9709753.139463063

FEE = 3_000          # $3,000/yr separately charged by the discretionary manager
PORTFOLIO = 500_000  # the non-reg portfolio it manages (declared on the doc)


def _doc(fee=FEE):
    """The p1/p2 two-generation subset of the shipped example, with a $3,000/yr
    management fee declared on the jointly-owned non-registered account."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = _two_generation_subset(json.load(fh))
    for acc in doc["accounts"]:
        if acc["kind"] == "non_reg":
            acc["balance"]["amount"] = PORTFOLIO
            if fee is not None:
                acc["deductible_management_fee_annual"] = fee
    return doc


def _run(doc, years=None):
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


# ============================================================================
# 1. The pure mapper: one leg per year, attributed to the joint owners
# ============================================================================

class TestMapManagementFeeLegs(unittest.TestCase):

    def test_one_negative_leg_per_year_through_the_horizon(self):
        legs = map_management_fee_legs(_doc(), 2026)
        # Horizon person p1 born 1980 projected to age 95 -> final year 2075.
        self.assertEqual([l["year"] for l in legs], list(range(2026, 2076)))
        self.assertEqual({l["amount"] for l in legs}, {-float(FEE)})

    def test_legs_are_post_tax_costs_named_by_account(self):
        legs = map_management_fee_legs(_doc(), 2026)
        self.assertEqual(legs[0]["tax_treatment"], "post-tax")
        self.assertEqual(legs[0]["kind"], "cost")
        self.assertEqual(legs[0]["id"], "joint_nonreg")
        self.assertEqual(legs[0]["label"], "non-registered management fee")

    def test_no_fee_produces_no_legs(self):
        self.assertEqual(map_management_fee_legs(_doc(fee=None), 2026), [])

    def test_explicit_zero_is_a_real_zero_not_a_drop(self):
        legs = map_management_fee_legs(_doc(fee=0), 2026)
        self.assertTrue(legs)
        self.assertTrue(all(l["amount"] == 0.0 for l in legs))

    def test_legs_join_the_dated_cash_flow_channel(self):
        doc = _doc()
        cfg = ic.to_internal_config(doc)
        legs = [cf for cf in cfg["cash_flows"]
                if cf.get("label") == "non-registered management fee"]
        self.assertEqual([(l["year"], l["amount"]) for l in legs],
                         [(y, -float(FEE)) for y in range(2026, 2076)])

    def test_no_fee_maps_cash_flows_exactly_as_before(self):
        baseline = ic.to_internal_config(_doc())["cash_flows"]
        stripped = ic.to_internal_config(_doc(fee=None))["cash_flows"]
        self.assertEqual(stripped, [cf for cf in baseline
                                    if cf.get("label")
                                    != "non-registered management fee"])

    def test_joint_fee_attributed_pro_rata_to_each_owner(self):
        members = ic.to_internal_config(_doc())["family"]["members"]
        fees = {m["role"]: m.get("mgmt_fee_non_reg_annual")
                for m in members}
        # joint_nonreg is owned 50/50 by p1/p2: each deducts their own half.
        self.assertEqual(fees.get("primary"), FEE / 2)
        self.assertEqual(fees.get("spouse"), FEE / 2)


# ============================================================================
# 2. The contract boundary: schema accepts non-reg, refuses registered
# ============================================================================

class TestContractBoundary(unittest.TestCase):

    def test_schema_validates_a_declared_non_reg_fee(self):
        validate_contract(_doc())

    def test_schema_refuses_a_negative_fee(self):
        doc = _doc(fee=-100)
        with self.assertRaises(Exception):
            validate_contract(doc)

    def test_registered_account_fee_is_refused_loudly(self):
        """s.20(1)(e) covers NON-REGISTERED investments only -- a fee inside an
        RRSP is not deductible, and this field is not its spelling. DP#32:
        refuse loudly, never silently treat as non-deductible cash."""
        doc = _doc()
        doc["accounts"][0]["deductible_management_fee_annual"] = 500
        with self.assertRaises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_fee_outside_the_simulated_couple_is_refused_loudly(self):
        """An additional accumulating adult's fee has no tax seam to reach
        (#899) -- silently dropping it would price a phantom deduction."""
        doc = _doc()
        doc["people"].append(
            {"id": "xa", "label": "Extra adult",
             "birth_date": "1990-01-01", "death_date": None,
             "residency": {"province": "quebec", "since": "1990-01-01"},
             "relationships": [{"type": "parent_of", "person": "p1"}],
             "incomes": [],
             "room": {"rrsp": None, "tfsa": None, "fhsa": None,
                      "resp": None}})
        doc["accounts"].append(
            {"id": "xa_nonreg", "owner": "xa", "kind": "non_reg",
             "balance": {"amount": 10000, "as_of": "2026-01-01"},
             "acb": None, "holdings": [], "beneficiary": None,
             "successor_holder": None,
             "deductible_management_fee_annual": 100})
        with self.assertRaises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)


# ============================================================================
# 3. The working-phase fold: real cash out, bracket-aware deduction back
# ============================================================================

class TestAccumulationFold(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.baseline = _run(_doc(fee=None), years=2)
        cls.with_fee = _run(_doc(), years=2)

    def test_fee_leaves_the_savings_channel_exactly(self):
        """The dated leg is REAL cash: year 0's savings channel drops by the
        full declared fee -- the deduction never nets against this channel
        (the tax side books through after-tax income, tested next)."""
        self.assertAlmostEqual(
            self.baseline[0].annual_savings - self.with_fee[0].annual_savings,
            float(FEE), places=2)

    def test_deduction_saves_bracket_fill_not_flat_top_rate(self):
        """Each owner's taxable income falls by THEIR pro-rata share; the
        after-tax income rises by exactly the bracket-fill tax difference
        (tax_on_income(I) - tax_on_income(I - share)) -- NOT share x top
        marginal rate (the #1033 bracket-fill discipline).

        The shipped example's own private-loan facts give the couple's
        PRE-fee taxable incomes: $118,000 - $500 (primary's s.20(1)(c)
        deduction) = $117,500, and $96,000 + $500 (spouse's interest
        income) = $96,500. Each owner's share of the joint fee is $1,500.
        """
        brackets = default_tax_provider().get_combined_brackets(
            2026, "quebec")
        expected = sum(
            tax_on_income(income, brackets)
            - tax_on_income(income - FEE / 2, brackets)
            for income in (117_500, 96_500))
        actual = (self.with_fee[0].after_tax_income
                  - self.baseline[0].after_tax_income)
        self.assertAlmostEqual(actual, expected, places=2)
        top_rate = max(b["rate"] for b in brackets)
        self.assertLess(actual, FEE * top_rate,
                        "bracket-fill saving must be below the flat-top-rate "
                        "product when the deduction does not fill the top "
                        "bracket alone")

    def test_no_fee_declares_no_member_key(self):
        """DP#32 at the adapter: a household declaring no fee carries no
        mgmt-fee key on any member record -- absence, never a planted zero."""
        members = ic.to_internal_config(_doc(fee=None))["family"]["members"]
        self.assertTrue(all("mgmt_fee_non_reg_annual" not in m
                            for m in members))


# ============================================================================
# 4. Composition with #136: embedded MER drags growth; the separate fee is
#    deducted AND paid. Both declared -> BOTH apply, neither doubled.
# ============================================================================

class TestCompositionWithMer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.base = _doc(fee=None)
        for acc in cls.base["accounts"]:
            if acc["kind"] == "non_reg":
                acc["mer"] = 0.01
        cls.mer_only = _run(cls.base, years=2)
        cls.base_after_tax_income = _run(_doc(fee=None), years=2)[0].after_tax_income
        cls.fee_only = _run(_doc(), years=2)
        both = _doc()  # fee + mer
        for acc in both["accounts"]:
            if acc["kind"] == "non_reg":
                acc["mer"] = 0.01
        cls.both = _run(both, years=2)

    def test_both_spellings_reach_the_engine_without_conflating(self):
        """#142 vs #691/#136: the two fee spellings are different facts about
        the same account and BOTH reach the engine's config when both are
        declared -- the embedded MER as the pot-keyed drag structure, the
        separate s.20(1)(e) fee on the owner's member record. Neither
        swallows the other.

        (In THIS fixture the non-reg pot compounds through the DP#27
        portfolio-composition path, whose embedded product MERs --
        assumptions.products.*.mer -- are what drag its growth; the
        account-level ``mer`` blend applies on the flat-rate fallback path,
        pre-existing #691/#136 behaviour outside this issue's scope.)"""
        doc = _doc()
        for acc in doc["accounts"]:
            if acc["kind"] == "non_reg":
                acc["mer"] = 0.01
        cfg_dict = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(cfg_dict)
        drag = sim_cfg.account_mer_drag.get("non_reg")
        self.assertTrue(drag and drag.get("fee_rate") == 0.01)
        fees = {m["role"] for m in sim_cfg.family_members
                if m.get("mgmt_fee_non_reg_annual") is not None}
        self.assertEqual(fees, {"primary", "spouse"})

    def test_fee_still_deducted_when_mer_also_declared(self):
        # The fee's tax saving is UNCHANGED by also declaring an embedded MER:
        # the deduction prices the separately-charged fee only, never the
        # embedded one (no double-charge in either direction).
        self.assertAlmostEqual(
            self.both[0].after_tax_income - self.mer_only[0].after_tax_income,
            self.fee_only[0].after_tax_income - self.base_after_tax_income,
            places=4,
            msg="the fee's deduction must be identical whether or not the "
                "funds' embedded MER is also declared")


# ============================================================================
# 5. The retirement phase: the fee comes off the OAS-clawback base at the
#    source, before plan_drawdown_net's bounded fixpoint reads it.
# ============================================================================

from rule_registry import RULES, RuleContext, YearWorkingState  # noqa: E402

START_YEAR = 2026


def _run_sizing_rule(members, retirement, sim_year=START_YEAR,
                     primary_retired=True, spouse_retired=True):
    """Drive the ``retirement_income`` rule in isolation (the
    test_issue_363_pr4 harness): returns the populated ``YearWorkingState``."""
    cfg_dict = {
        'assumptions': {'start_year': START_YEAR, 'investment_return': 0.05,
                        'inflation': 0.02, 'horizon_age': 95},
        'property': {'house_value': 500000, 'mortgage_balance': 0,
                     'margin_available': 0, 'ltv_max': 0.80,
                     'amortization_years': 25, 'mortgage_rate': 0.045},
        'family': {'members': members, 'children': []},
        'accounts': {'rrsp_annual_max': 31000},
        'retirement': retirement,
        'tax': {'province': 'qc'},
    }
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg)
    ws = YearWorkingState(year=sim_year - sim.start_year)
    ctx = RuleContext(
        year=sim_year - sim.start_year, calendar_year=sim_year, allocations={},
        config=cfg, investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
        mortgage_data=None, use_readvanceable=False, deduct_later=False,
        primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
        fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
        fhsa_annual_limit=None, non_reg_after_tax_return=None,
        cpp_income=0.0, oas_income=0.0, pension_income=0.0, drawdown_order=None,
        rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
        drawdown_net_target=0.0,
        retiree_marginal_rate=0.0, drawdown_bracket_target=None,
        drawdown_other_taxable_income=0.0,
        primary_income_pre=0.0, spouse_income_pre=0.0,
        primary_retired=primary_retired, spouse_retired=spouse_retired,
        base_primary_income=sim._primary_income,
        base_spouse_income=sim._spouse_income,
        year_brackets=sim.brackets,
        tax_indexation_rate=sim.tax_provider.indexation_rate,
    )
    fired = RULES['retirement_income'](ws, ctx)
    return ws, fired


_RETIREE_P = {'role': 'primary', 'gross_income': 0,
              'birth_year': START_YEAR - 70, 'retirement_age': 65,
              'cpp_monthly_estimated': 100, 'pension_income_annual': 30000,
              'rrsp_balance': 400000}
_RETIREE_S = {'role': 'spouse', 'gross_income': 0,
              'birth_year': START_YEAR - 68, 'retirement_age': 65,
              'cpp_monthly_estimated': 100, 'pension_income_annual': 8000,
              'rrsp_balance': 200000}


class TestRetirementClawbackBase(unittest.TestCase):

    def test_retired_members_fee_reduces_their_own_base(self):
        ws, _ = _run_sizing_rule(
            [dict(_RETIREE_P, mgmt_fee_non_reg_annual=3000),
             dict(_RETIREE_S, mgmt_fee_non_reg_annual=2000)],
            {'spending_target': 60000})
        self.assertAlmostEqual(ws.drawdown_other_taxable_income_primary,
                               30000 + 1200 - 3000, places=6)
        self.assertAlmostEqual(ws.drawdown_other_taxable_income_spouse,
                               8000 + 1200 - 2000, places=6)
        # Household base reduced by the SUM, and the OAS-inclusive bracket-fill
        # base follows the reduced progressive base.
        self.assertAlmostEqual(ws.drawdown_other_taxable_income,
                               30000 + 8000 + 2400 - 5000, places=6)
        self.assertAlmostEqual(ws.drawdown_bracket_fill_base_primary,
                               ws.drawdown_other_taxable_income_primary
                               + ws.drawdown_oas_gross_primary, places=6)

    def test_base_floors_at_zero_excess_strands(self):
        """A fee larger than the base strands the excess (a non-capital loss /
        carry-forward, not modeled) -- it never drives the base negative."""
        ws, _ = _run_sizing_rule(
            [dict(_RETIREE_S, mgmt_fee_non_reg_annual=50_000)],
            {'spending_target': 60000})
        self.assertEqual(ws.drawdown_other_taxable_income_spouse, 0.0)

    def test_working_member_fee_not_applied_here(self):
        """The WORKING spouse's fee fires in the prologue (their employment
        income exists there); exactly ONE mechanism per phase -- the rule must
        leave their (zero anyway) and the household arithmetic untouched by
        it, and must not deduct the retired member's fee twice."""
        ws, _ = _run_sizing_rule(
            [dict(_RETIREE_P, mgmt_fee_non_reg_annual=3000),
             dict(_RETIREE_S, mgmt_fee_non_reg_annual=2000)],
            {'spending_target': 60000},
            primary_retired=True, spouse_retired=False)
        self.assertAlmostEqual(ws.drawdown_other_taxable_income_spouse, 0.0,
                               places=6)
        self.assertAlmostEqual(ws.drawdown_other_taxable_income,
                               30000 + 1200 - 3000, places=6)

    def test_no_fee_leaves_bases_identical(self):
        plain, _ = _run_sizing_rule([_RETIREE_P, _RETIREE_S],
                                    {'spending_target': 60000})
        declared, _ = _run_sizing_rule(
            [_RETIREE_P, dict(_RETIREE_S, mgmt_fee_non_reg_annual=0)],
            {'spending_target': 60000})
        self.assertAlmostEqual(plain.drawdown_other_taxable_income,
                               declared.drawdown_other_taxable_income,
                               places=6)


# ============================================================================
# 6. The AMT wiring: the (j)(ii) half-add-back references the declared fee.
# ============================================================================

from simulation_state import SimState, simulate_year_pure, _default_canada_state  # noqa: E402
from countries.canada.amt import AMTParameters, total_tax_with_amt  # noqa: E402
from countries.canada.tax_calc import (  # noqa: E402
    compute_non_refundable_credits, federal_tax_before_abatement,
    quebec_abatement_amount,
)


def _amt_run(fee):
    """One retirement year realizing a large capital gain from the non-reg
    pot, with (fee > 0) or without (fee == 0) a declared management fee."""
    config = SimulationConfig(
        projection_years=1, investment_return=0.05, mortgage_balance=0,
        mortgage_rate=0.05, margin_available=0, province='quebec',
        family_members=[
            {'role': 'primary', 'gross_income': 0, 'birth_year': 1955,
             'mgmt_fee_non_reg_annual': fee},
            {'role': 'spouse', 'gross_income': 0, 'birth_year': 1957},
        ],
        children=[],
    )
    state = SimState(
        non_reg_balance=3_000_000, non_reg_acb=0.0,
        jurisdiction_state={'canada': _default_canada_state()},
    )
    result, _ = simulate_year_pure(
        state=state, year=0,
        allocations={'_primary_income': 0, '_annual_savings': 0},
        config=config, investment_return=0.05,
        primary_marginal_rate=0.53, retiree_marginal_rate=0.53,
        calendar_year=2026,
        drawdown_net_target=1_200_000, drawdown_order=['non_reg'],
        any_retired=True, retirement_spending_target=1_200_000,
    )
    return result


def _expected_surcharge(taxable_income, realized_gain, fee,
                        province='quebec', year=2026):
    """The federal surcharge straight from the amt module, WITH the #142
    alignment: the fee netted out of taxable income, then half added back via
    ``carrying_charges`` (s.127.52(1)(j)(ii))."""
    provider = default_tax_provider()
    ti = max(0.0, taxable_income - fee)
    gross_fed = federal_tax_before_abatement(ti, year, province, provider)
    abatement = quebec_abatement_amount(ti, year, province, provider)
    nr = compute_non_refundable_credits(0, ti, year, province, provider)['total']
    return total_tax_with_amt(
        regular_tax=max(0.0, gross_fed - abatement - nr),
        taxable_income=ti,
        taxable_capital_gains=0.5 * realized_gain,
        capital_gains_inclusion=0.5,
        carrying_charges=fee,
        nonrefundable_credits=nr,
        params=AMTParameters.for_year(year, provider),
    )['amt_surcharge']


class TestAmtWiring(unittest.TestCase):

    def test_surcharge_matches_the_half_add_back_oracle(self):
        r = _amt_run(FEE)
        self.assertGreater(r.amt_surcharge, 0)
        self.assertAlmostEqual(
            r.amt_surcharge,
            _expected_surcharge(r.drawdown_taxable, r.realized_capital_gains,
                                FEE),
            places=4)

    def test_fee_changes_the_assessment_versus_no_fee(self):
        """The wiring is live: the same realization with the fee declared
        prices a DIFFERENT minimum amount than without it."""
        no_fee = _amt_run(0)
        with_fee = _amt_run(FEE)
        self.assertAlmostEqual(
            with_fee.amt_surcharge,
            _expected_surcharge(with_fee.drawdown_taxable,
                                with_fee.realized_capital_gains, FEE),
            places=4)
        self.assertNotAlmostEqual(
            no_fee.amt_surcharge, with_fee.amt_surcharge, places=4)


# ============================================================================
# 7. The crux: golden household declares no fee -> byte-identical.
# ============================================================================

class TestGoldenNoOp(unittest.TestCase):

    def test_golden_household_is_byte_identical_with_no_fee(self):
        results = _run_golden(golden_household_config())
        terminal = results[-1].total_assets
        self.assertEqual(
            terminal, TERMINAL_TOTAL_ASSETS,
            f"golden terminal total_assets MOVED: {terminal!r} != "
            f"{TERMINAL_TOTAL_ASSETS!r}")


if __name__ == "__main__":
    unittest.main()
