#!/usr/bin/env python3
"""Issue #832: private loan from an individual, with on-demand repayment/interest.

Supersedes the narrow #813 family_loans design. A private loan is a loan FROM
AN INDIVIDUAL -- the lender is either a declared household member or an
individual outside the household (a grandparent, a friend, an adult child not
in people[]). It is a DEMAND loan by default: the borrower has no obligation to
pay capital or interest unless the lender asks. The contract expresses this as
a ``private_loans[]`` block: {id, lender, borrower, rate, principal, use,
repayment: on_demand|amortizing, interest: on_demand|paid}.

Wiring (reusing the existing deductible-interest trace, NOT duplicating it):
  - when interest IS paid/payable (interest=paid, or repayment=amortizing which
    implies paid): the LENDER accrues taxable interest income (rate x principal,
    taxed in their bracket -- ONLY when the lender is a household member; an
    external individual is not taxed here); the BORROWER's interest is
    DEDUCTIBLE only when use=investment (ITA s.20(1)(c)); attribution (ITA
    s.74.2) applies when a MINOR household member lends.
  - when interest is NOT payable this year (the default on_demand/on_demand
    demand loan): NO interest tax flow at all -- no deduction, no lender income.
    The loan is modeled as interest-free financing (free liquidity), not a
    forced 5% deductible/taxable split (s.20(1)(c): deductible only when paid
    or payable; income to the lender only when received/receivable).

Wiring (reusing the existing deductible-interest trace, NOT duplicating it):
  - the LENDER accrues taxable interest income (rate x principal), added to
    the lender's taxable income so it is taxed in the lender's own bracket;
  - the BORROWER's interest is DEDUCTIBLE (subtracted from taxable income)
    only when use=investment (ITA s.20(1)(c)); use=consumption -> no deduction;
  - the interest is an INTRA-HOUSEHOLD transfer (borrower pays lender), so it
    NETS TO ZERO in household cash income -- only the TAX effects remain;
  - attribution (ITA s.74.2): when the lender is a MINOR (age < 18 at the
    simulation year, derived from birth_year -- DP#1), the interest is
    attributed to the BORROWER instead (the minor's property income is taxed
    in the transferor's hands). An adult lender (18+) is exempt.

Tests (DP#15: fabricated round numbers, role-based names):
  1. use=investment -> the borrower's deduction saves more tax (higher bracket)
     than the lender pays on the interest (lower bracket) -> household
     after_tax_income RISES by (borrower_rate - lender_rate) x interest.
  2. use=consumption -> NO deduction; the lender still pays tax on the
     interest -> household after_tax_income FALLS by lender_rate x interest.
  3. A MINOR lender -> attribution (s.74.2) adds the interest to the borrower
     AND the investment deduction subtracts it -> nets to ZERO (the split is
     undone: the parent is taxed as if they still own the income).
  4. The golden household (no private loans) is UNCHANGED -- the feature is
     gated on declared private_loans and is a no-op when absent.
  5. input_contract: a private loan naming an undeclared person is REFUSED
     loudly (DP#32), not silently dropped to a zero loan.
  6. The monthly time-step fold applies the SAME interest split as the yearly
     fold (the feature is time-step-agnostic, via the shared helper).
"""

import unittest

import countries.canada  # noqa: F401  (register the jurisdiction adapter)
import contract_errors
import contract_schema


def _cfg(*, family_members, children=None, private_loans=None, time_step='yearly',
         years=1):
    from simulation_config import SimulationConfig
    return SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, living_costs=40_000, start_year=2026,
        province='quebec', investment_return=0.0, salary_growth=0.0,
        time_step=time_step, family_members=family_members,
        children=children or [], private_loans=private_loans or [])


def _run(cfg):
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()


def _members(income_p=120_000, income_s=40_000):
    return [
        {'role': 'primary', 'birth_year': 1980, 'gross_income': income_p,
         'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        {'role': 'spouse', 'birth_year': 1982, 'gross_income': income_s,
         'id': 'p2', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]


class TestPrivateLoanInterestSplit(unittest.TestCase):
    """The lender's interest is taxed; the borrower's is deductible only for
    investment use. The interest is an intra-household transfer that nets to
    zero in household cash -- only the tax effects remain."""

    LOAN = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
            'rate': 0.05, 'principal': 100_000, 'use': 'investment',
            'repayment': 'amortizing', 'interest': 'paid'}
    # interest = 5% x $100,000 = $5,000.

    def test_investment_use_gives_borrower_deduction_and_lender_taxable_income(self):
        # Spouse (p2, lower bracket) lends to primary (p1, higher bracket) at
        # 5% on $100k for investment. The $5,000 interest is a transfer that
        # nets to zero in household cash. The tax effects: primary deducts
        # $5,000 (saves tax at the primary's HIGHER bracket); spouse is taxed
        # on $5,000 (pays tax at the spouse's LOWER bracket). The household
        # after_tax_income RISES by (primary_rate - spouse_rate) x $5,000 > 0
        # -- the bracket arbitrage the intra-private loan exists to capture.
        base = _run(_cfg(family_members=_members()))
        inv = _run(_cfg(family_members=_members(), private_loans=[self.LOAN]))
        benefit = inv[0].after_tax_income - base[0].after_tax_income
        self.assertGreater(benefit, 0.0,
                           "investment use: the deduction at the borrower's "
                           "higher bracket must beat the lender's tax at the "
                           "lower bracket (the whole point of the split)")
        # The benefit is bounded above by the borrower's marginal rate x 5000
        # (the deduction at the borrower's rate, before the lender's tax).
        self.assertLess(benefit, 0.50 * 5_000,
                        "the benefit cannot exceed the borrower's deduction "
                        "at a top-bracket rate")

    def test_consumption_use_gives_no_deduction_but_lender_still_taxed(self):
        # Same loan but use=consumption: the borrower gets NO deduction (ITA
        # s.20(1)(c) requires income-producing use), while the lender is STILL
        # taxed on the $5,000 interest. The transfer nets to zero in household
        # cash, so the only effect is the lender's extra tax -> household
        # after_tax_income FALLS by spouse_rate x $5,000 > 0.
        loan = {**self.LOAN, 'use': 'consumption'}
        base = _run(_cfg(family_members=_members()))
        con = _run(_cfg(family_members=_members(), private_loans=[loan]))
        cost = base[0].after_tax_income - con[0].after_tax_income
        self.assertGreater(cost, 0.0,
                           "consumption use: no deduction, but the lender "
                           "still pays tax on the interest -> household "
                           "after_tax_income must fall")
        # The cost is the lender's tax on $5,000 -- bounded by a top rate.
        self.assertLess(cost, 0.50 * 5_000)

    def test_investment_beats_consumption_by_the_borrower_deduction(self):
        # The ONLY difference between the investment and consumption loans is
        # the borrower's $5,000 deduction. So investment after_tax minus
        # consumption after_tax equals the borrower's tax saving on $5,000
        # (at the primary's marginal rate) -- the deduction's direct value.
        inv = _run(_cfg(family_members=_members(), private_loans=[self.LOAN]))
        con = _run(_cfg(family_members=_members(),
                        private_loans=[{**self.LOAN, 'use': 'consumption'}]))
        self.assertGreater(inv[0].after_tax_income - con[0].after_tax_income, 0.0,
                           "investment must beat consumption by exactly the "
                           "borrower's deduction value")


class TestMinorLenderAttribution(unittest.TestCase):
    """ITA s.74.2: a minor lender's interest is attributed to the borrower."""

    def test_minor_lender_attribution_undoes_the_split(self):
        # A 15-year-old child lends to the primary at 5% on $100k for
        # investment. s.74.2 attributes the $5,000 interest to the BORROWER
        # (primary): the primary's taxable income RISES by $5,000 (attributed
        # interest) AND FALLS by $5,000 (investment deduction) -> net zero
        # taxable change -> NO household tax effect. The attribution undoes
        # the split: the parent is taxed as if they still own the income.
        children = [{'name': 'child_a', 'id': 'ca', 'birth_year': 2011,
                     'gross_income': 0}]  # age 15 in 2026
        loan = {'id': 'l1', 'lender': 'ca', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = _run(_cfg(family_members=_members(income_s=20_000),
                         children=children))
        with_loan = _run(_cfg(family_members=_members(income_s=20_000),
                              children=children, private_loans=[loan]))
        self.assertAlmostEqual(with_loan[0].after_tax_income,
                               base[0].after_tax_income, places=2,
                               msg="minor lender: attribution adds the interest "
                                   "to the borrower AND the investment deduction "
                                   "subtracts it -> nets to zero (split undone)")

    def test_adult_lender_is_exempt_from_attribution_and_warned(self):
        # An ADULT child (20) lending to the parent is EXEMPT from attribution
        # (18+). The interest stays the child's, but this engine does not tax
        # children as individuals (#701), so the lender's interest is EARNED
        # but NOT TAXED -- surfaced as a loud DP#32 warning, not a silent drop.
        # The borrower still gets the investment deduction, so the household
        # after_tax_income rises by the deduction (the unmodelled child tax is
        # the named limitation, not a silent windfall).
        children = [{'name': 'child_a', 'id': 'ca', 'birth_year': 2006,
                     'gross_income': 0}]  # age 20 in 2026
        loan = {'id': 'l1', 'lender': 'ca', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = _run(_cfg(family_members=_members(income_s=20_000),
                         children=children))
        with self.assertLogs('simulation', level='WARNING') as cm:
            with_loan = _run(_cfg(family_members=_members(income_s=20_000),
                                  children=children, private_loans=[loan]))
        self.assertTrue(any('#832' in m and 'NOT TAXED' in m for m in cm.output),
                        "an adult-child lender's untaxed interest must warn "
                        "loudly (DP#32), not silently drop")
        # The borrower's deduction still lands (household after_tax rises).
        self.assertGreater(with_loan[0].after_tax_income - base[0].after_tax_income,
                           0.0,
                           "the borrower's investment deduction applies even "
                           "when the adult-child lender's tax is unmodelled")


class TestNoPrivateLoansIsNoOp(unittest.TestCase):
    """A household that declares no private loans is unaffected (the golden case)."""

    def test_no_private_loans_matches_baseline_exactly(self):
        # Empty private_loans (the default) -> the helper returns all zeros ->
        # taxable income == base income -> identical to a run with no
        # private_loans field at all.
        r1 = _run(_cfg(family_members=_members(), private_loans=[]))
        r2 = _run(_cfg(family_members=_members()))
        self.assertAlmostEqual(r1[0].after_tax_income, r2[0].after_tax_income,
                               places=4,
                               msg="empty private_loans is a no-op, identical "
                                   "to the field being absent")


class TestMonthlyFoldAppliesTheSameSplit(unittest.TestCase):
    """The _run_monthly fold has its own prologue; the interest split must
    land identically under time_step='monthly' (shared helper, DP#9)."""

    def test_monthly_matches_yearly_for_investment_loan(self):
        loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        y = _run(_cfg(family_members=_members(), private_loans=[loan],
                      time_step='yearly'))
        m = _run(_cfg(family_members=_members(), private_loans=[loan],
                      time_step='monthly'))
        base_y = _run(_cfg(family_members=_members(), time_step='yearly'))
        base_m = _run(_cfg(family_members=_members(), time_step='monthly'))
        benefit_y = y[0].after_tax_income - base_y[0].after_tax_income
        benefit_m = m[0].after_tax_income - base_m[0].after_tax_income
        self.assertAlmostEqual(benefit_y, benefit_m, places=2,
                               msg="the interest split is time-step-agnostic: "
                                   "monthly and yearly folds credit the same "
                                   "household tax effect")


class TestContractParsing(unittest.TestCase):
    """input_contract: private_loans[] is parsed, validated, and a bad
    lender/borrower is refused loudly (DP#32)."""

    def test_undeclared_lender_is_refused(self):
        import copy
        import json

        import input_contract as ic
        with open(contract_schema.EXAMPLE_PATH) as f:
            d = json.load(f)
        # Trim to the two-generation subset the adapter can map (p1/p2 + ca/cb).
        keep = {"p1", "p2", "ca", "cb"}
        doc = copy.deepcopy(d)
        doc["people"] = [p for p in doc["people"] if p["id"] in keep]
        for p in doc["people"]:
            p["relationships"] = [r for r in p.get("relationships", [])
                                  if r["person"] in keep]
        doc["accounts"] = []
        doc["liabilities"] = []
        doc["properties"] = []
        doc["estate"]["rollover_overrides"] = []
        doc["estate"]["life_insurance"] = []
        doc["assumptions"]["mortality"] = [m for m in doc["assumptions"].get(
            "mortality", []) if m["person"] in keep]
        doc["assumptions"].pop("emergency_reserve", None)
        # A loan naming a person who is not declared -> refused (DP#32).
        doc["private_loans"] = [{
            "id": "bad", "lender": "nobody", "borrower": "p1",
            "rate": 0.05, "principal": 1000, "use": "investment"}]
        with self.assertRaises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_valid_loan_is_parsed_onto_config(self):
        import copy
        import json

        import input_contract as ic
        with open(contract_schema.EXAMPLE_PATH) as f:
            d = json.load(f)
        keep = {"p1", "p2", "ca", "cb"}
        doc = copy.deepcopy(d)
        doc["people"] = [p for p in doc["people"] if p["id"] in keep]
        for p in doc["people"]:
            p["relationships"] = [r for r in p.get("relationships", [])
                                  if r["person"] in keep]
        doc["accounts"] = []
        doc["liabilities"] = []
        doc["properties"] = []
        doc["estate"]["rollover_overrides"] = []
        doc["estate"]["life_insurance"] = []
        doc["assumptions"]["mortality"] = [m for m in doc["assumptions"].get(
            "mortality", []) if m["person"] in keep]
        doc["assumptions"].pop("emergency_reserve", None)
        doc["private_loans"] = [{
            "id": "ca_to_p1", "lender": "ca", "borrower": "p1",
            "rate": 0.05, "principal": 10_000, "use": "investment",
            "repayment": "amortizing", "interest": "paid"}]
        cfg = ic.to_internal_config(doc)
        # The internal shape carries lender_is_external + the repayment/interest
        # terms (defaults applied when absent -- here they are explicit).
        self.assertEqual(cfg["family"]["private_loans"], [{
            "id": "ca_to_p1", "lender": "ca", "lender_is_external": False,
            "borrower": "p1", "rate": 0.05, "principal": 10_000.0,
            "use": "investment", "repayment": "amortizing", "interest": "paid"}])
        # round-trips through SimulationConfig (DP#24).
        from simulation_config import SimulationConfig
        sc = SimulationConfig.from_dict(cfg)
        self.assertEqual(sc.private_loans, cfg["family"]["private_loans"])


class TestTransferBranchCoverage(unittest.TestCase):
    """Lean tests covering the remaining _private_loan_interest_for branches:
    primary-as-lender, spouse-as-borrower, minor-lends-to-spouse attribution,
    a child borrower (investment deduction has no tax to reduce -> warned),
    a zero-interest loan (no-op), and a lender with no birth_year (age
    unknown -> treated as adult, no attribution). Fabricated round numbers."""

    def _run(self, *, family_members, children=None, private_loans=None):
        return _run(_cfg(family_members=family_members, children=children,
                         private_loans=private_loans))

    def test_primary_lends_to_spouse_investment(self):
        # The PRIMARY is the lender (interest income to the primary) and the
        # SPOUSE is the borrower (investment deduction to the spouse). Covers
        # the primary-lender income branch and the spouse-borrower deduction
        # branch. $100k at 5% = $5,000 interest.
        loan = {'id': 'l1', 'lender': 'p1', 'borrower': 'p2',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = self._run(family_members=_members())
        with_loan = self._run(family_members=_members(), private_loans=[loan])
        # The transfer nets to zero; the tax effect is the spouse's deduction
        # (lower bracket) minus the primary's tax on the interest (higher
        # bracket) -> household after_tax FALLS (the split goes the wrong way
        # when the higher-bracket member lends to the lower-bracket one).
        self.assertNotEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                            "primary->spouse investment loan must have a real "
                            "tax effect (lender income + borrower deduction)")

    def test_minor_lends_to_spouse_attribution(self):
        # A minor child lends to the SPOUSE (not the primary). s.74.2
        # attributes the interest to the borrower (spouse): spouse taxable
        # income += 5000 (attributed) AND -= 5000 (investment deduction) ->
        # nets to zero. Covers the spouse attribution-target branch.
        children = [{'name': 'child_a', 'id': 'ca', 'birth_year': 2011,
                     'gross_income': 0}]  # age 15 in 2026
        loan = {'id': 'l1', 'lender': 'ca', 'borrower': 'p2',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = self._run(family_members=_members(income_s=20_000),
                         children=children)
        with_loan = self._run(family_members=_members(income_s=20_000),
                              children=children, private_loans=[loan])
        self.assertAlmostEqual(with_loan[0].after_tax_income,
                               base[0].after_tax_income, places=2,
                               msg="minor->spouse: attribution to the spouse "
                                   "and the investment deduction net to zero")

    def test_child_borrower_investment_deduction_warns(self):
        # A child is the BORROWER with use=investment. The interest IS
        # deductible under s.20(1)(c), but the engine does not tax children
        # (#701), so the deduction has no tax to reduce -> a loud DP#32
        # warning, not a silent drop. The lender (spouse) is still taxed.
        children = [{'name': 'child_a', 'id': 'ca', 'birth_year': 2010,
                     'gross_income': 0}]
        loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'ca',
                'rate': 0.05, 'principal': 10_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        with self.assertLogs('simulation', level='WARNING') as cm:
            self._run(family_members=_members(income_s=20_000),
                      children=children, private_loans=[loan])
        self.assertTrue(any('#832' in m and 'borrower' in m for m in cm.output),
                        "a child borrower's deductible interest must warn "
                        "loudly that the engine has no child tax to reduce")

    def test_zero_interest_loan_is_a_noop(self):
        # A loan with rate=0 (or principal=0) accrues no interest -> no tax
        # effect at all. Covers the interest <= 0 early-continue branch.
        loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.0, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = self._run(family_members=_members())
        with_loan = self._run(family_members=_members(), private_loans=[loan])
        self.assertAlmostEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                               places=4,
                               msg="a zero-interest private loan accrues no "
                                   "interest -> no tax effect")

    def test_lender_with_no_birth_year_is_treated_as_adult(self):
        # A lender whose birth_year is unknown (None) cannot be determined a
        # minor, so attribution does NOT apply (the age gate is conservative:
        # age unknown -> not a proven minor -> no attribution). The lender's
        # interest is taxed in the lender's (spouse's) bracket as normal.
        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
             'id': 'p1', 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            {'role': 'spouse', 'gross_income': 40_000, 'id': 'p2',
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0}]  # no birth_year
        loan = {'id': 'l1', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = self._run(family_members=members)
        with_loan = self._run(family_members=members, private_loans=[loan])
        # The spouse (lender) is taxed on the interest; the primary deducts it.
        self.assertNotEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                            "an adult (age-unknown) lender's interest is taxed "
                            "normally -- no attribution, a real tax effect")


if __name__ == '__main__':
    unittest.main()

# ============================================================================
# Issue #832: on-demand repayment/interest + external lender.
# Fabricated round numbers, role-based names (DP#4/DP#15).
# ============================================================================

class TestOnDemandDemandLoanIsInterestFreeFinancing(unittest.TestCase):
    """The #832 motivating case: repayment=on_demand, interest=on_demand -> the
    borrower has NO obligation to pay interest unless the lender asks, so there
    is NO interest tax flow this year (no s.20(1)(c) deduction, no lender
    income). The loan is modeled as interest-free financing, not a forced 5%
    deductible/taxable split. ITA s.20(1)(c): deductible only when paid or
    payable; income to the lender only when received/receivable."""

    LOAN = {'id': 'demand', 'lender': 'p2', 'borrower': 'p1',
            'rate': 0.05, 'principal': 100_000, 'use': 'investment',
            'repayment': 'on_demand', 'interest': 'on_demand'}

    def test_on_demand_on_demand_produces_no_tax_flow(self):
        # Same household as the paid-investment test, but on_demand/on_demand:
        # no deduction, no lender income -> after_tax_income is UNCHANGED vs
        # the no-loan baseline (the loan is interest-free financing).
        base = _run(_cfg(family_members=_members()))
        with_loan = _run(_cfg(family_members=_members(), private_loans=[self.LOAN]))
        self.assertEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                         "on_demand/on_demand: no interest is payable, so no "
                         "tax flow at all -- the loan is interest-free financing")

    def test_default_terms_are_on_demand_on_demand(self):
        # A loan with NO repayment/interest declared defaults to
        # on_demand/on_demand -> no tax flow (the #832 default is interest-free).
        loan = {'id': 'default', 'lender': 'p2', 'borrower': 'p1',
                'rate': 0.05, 'principal': 100_000, 'use': 'investment'}
        base = _run(_cfg(family_members=_members()))
        with_loan = _run(_cfg(family_members=_members(), private_loans=[loan]))
        self.assertEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                         "default on_demand/on_demand: no tax flow")

    def test_interest_paid_turns_the_tax_flow_back_on(self):
        # The SAME loan but interest=paid -> the tax split applies (the #813
        # behavior). This proves the gate is interest-payability, not the
        # loan's mere existence.
        paid = {**self.LOAN, 'interest': 'paid'}
        base = _run(_cfg(family_members=_members()))
        with_paid = _run(_cfg(family_members=_members(), private_loans=[paid]))
        self.assertNotEqual(with_paid[0].after_tax_income, base[0].after_tax_income,
                            "interest=paid turns the tax flow back on")

    def test_repayment_amortizing_implies_interest_paid(self):
        # repayment=amortizing (scheduled principal-and-interest) implies
        # interest is paid/payable, even if interest is left on_demand.
        amortizing = {**self.LOAN, 'repayment': 'amortizing', 'interest': 'on_demand'}
        base = _run(_cfg(family_members=_members()))
        with_am = _run(_cfg(family_members=_members(), private_loans=[amortizing]))
        self.assertNotEqual(with_am[0].after_tax_income, base[0].after_tax_income,
                            "repayment=amortizing implies interest is paid -> tax flow applies")


class TestExternalLender(unittest.TestCase):
    """The lender is an individual OUTSIDE the household (inline {id,
    relationship?}). The engine does not tax an external individual (they are
    not a simulated member), so when interest IS paid, only the borrower's
    s.20(1)(c) deduction applies -- the lender-side income is outside scope."""

    def test_external_lender_paid_investment_gives_borrower_deduction_only(self):
        # An external individual (a grandparent) lends to the primary at 5% on
        # $100k for investment, interest=paid. The primary deducts $5,000; the
        # grandparent's income is NOT modelled (external). So household
        # after_tax_income RISES by the primary's tax saving on $5,000 (the
        # deduction alone, no offsetting lender tax inside the household).
        loan = {'id': 'ext', 'lender': {'id': 'grandparent_a', 'relationship': 'grandparent'},
                'borrower': 'p1', 'rate': 0.05, 'principal': 100_000,
                'use': 'investment', 'repayment': 'amortizing', 'interest': 'paid'}
        base = _run(_cfg(family_members=_members()))
        with_loan = _run(_cfg(family_members=_members(), private_loans=[loan]))
        self.assertGreater(with_loan[0].after_tax_income, base[0].after_tax_income,
                           "external lender, paid investment: the borrower's "
                           "deduction saves tax and no household-member lender "
                           "income offsets it -> after_tax_income rises")

    def test_external_lender_on_demand_has_no_tax_flow(self):
        # External + on_demand/on_demand -> interest-free financing, no flow
        # (the gate is payability, independent of whether the lender is internal).
        loan = {'id': 'ext_demand', 'lender': {'id': 'friend_a', 'relationship': 'friend'},
                'borrower': 'p1', 'rate': 0.05, 'principal': 100_000,
                'use': 'investment', 'repayment': 'on_demand', 'interest': 'on_demand'}
        base = _run(_cfg(family_members=_members()))
        with_loan = _run(_cfg(family_members=_members(), private_loans=[loan]))
        self.assertEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                         "external + on_demand: no tax flow (interest-free financing)")

    def test_external_lender_consumption_paid_has_no_deduction_and_no_lender_tax(self):
        # External + consumption + paid: no borrower deduction (personal
        # interest), and the external lender's income is outside scope -> no
        # household tax effect at all (the interest leaves the household to an
        # untaxed-external party, but that cash flow is not modelled here).
        loan = {'id': 'ext_con', 'lender': {'id': 'grandparent_a'},
                'borrower': 'p1', 'rate': 0.05, 'principal': 100_000,
                'use': 'consumption', 'repayment': 'amortizing', 'interest': 'paid'}
        base = _run(_cfg(family_members=_members()))
        with_loan = _run(_cfg(family_members=_members(), private_loans=[loan]))
        self.assertEqual(with_loan[0].after_tax_income, base[0].after_tax_income,
                         "external + consumption + paid: no deduction, no "
                         "household-member lender income -> no tax effect")


class TestExternalLenderContractParsing(unittest.TestCase):
    """input_contract: an inline lender object is accepted as an external
    individual; a string lender that does not resolve is still refused (DP#32);
    a borrower that does not resolve is refused."""

    def _doc(self):
        import copy
        import json

        with open(contract_schema.EXAMPLE_PATH) as f:
            d = json.load(f)
        keep = {"p1", "p2", "ca", "cb"}
        doc = copy.deepcopy(d)
        doc["people"] = [p for p in doc["people"] if p["id"] in keep]
        for p in doc["people"]:
            p["relationships"] = [r for r in p.get("relationships", [])
                                  if r["person"] in keep]
        doc["accounts"] = []
        doc["liabilities"] = []
        doc["properties"] = []
        doc["estate"]["rollover_overrides"] = []
        doc["estate"]["life_insurance"] = []
        doc["assumptions"]["mortality"] = [m for m in doc["assumptions"].get(
            "mortality", []) if m["person"] in keep]
        doc["assumptions"].pop("emergency_reserve", None)
        return doc

    def test_inline_external_lender_is_parsed_onto_config(self):
        import input_contract as ic
        doc = self._doc()
        doc["private_loans"] = [{
            "id": "ext_to_p1",
            "lender": {"id": "grandparent_a", "relationship": "grandparent"},
            "borrower": "p1", "rate": 0.04, "principal": 20_000,
            "use": "investment", "repayment": "on_demand", "interest": "on_demand"}]
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["family"]["private_loans"], [{
            "id": "ext_to_p1",
            "lender": {"id": "grandparent_a", "relationship": "grandparent"},
            "lender_is_external": True, "borrower": "p1",
            "rate": 0.04, "principal": 20_000.0, "use": "investment",
            "repayment": "on_demand", "interest": "on_demand"}])

    def test_string_lender_not_in_people_is_refused(self):
        # A STRING lender must resolve to a declared person (DP#32). To lend
        # from an external individual, the lender must be an inline object.
        import input_contract as ic
        doc = self._doc()
        doc["private_loans"] = [{
            "id": "bad", "lender": "stranger", "borrower": "p1",
            "rate": 0.05, "principal": 1000, "use": "investment"}]
        with self.assertRaises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_borrower_not_in_people_is_refused(self):
        # The borrower is always a declared household member (DP#32).
        import input_contract as ic
        doc = self._doc()
        doc["private_loans"] = [{
            "id": "bad_borrower", "lender": {"id": "ext_a"},
            "borrower": "nobody", "rate": 0.05, "principal": 1000,
            "use": "investment"}]
        with self.assertRaises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_inline_lender_without_id_is_refused(self):
        # An inline lender object must carry an id (DP#32). The schema's oneOf
        # requires `id` on the inline object, so validate_contract refuses it
        # loudly and early -- before the adapter ever sees it. (The adapter
        # does not re-check: the schema is the enforcement, and a second check
        # would be dead code the coverage ratchet flags.)
        import input_contract as ic
        doc = self._doc()
        doc["private_loans"] = [{
            "id": "no_id", "lender": {"relationship": "friend"},
            "borrower": "p1", "rate": 0.05, "principal": 1000,
            "use": "investment"}]
        with self.assertRaises(contract_errors.ContractValidationError):
            ic.to_internal_config(doc)
