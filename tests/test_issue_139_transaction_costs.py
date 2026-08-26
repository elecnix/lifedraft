#!/usr/bin/env python3
"""Issue #139: a GENERAL one-time transaction-cost/credit mechanism.

The engine prices recurring flows to the dollar but had NO concept of a
one-time transaction cost or credit attached to a financial event. Refinance
origination (discharge/quittance, notary/legal, title insurance, appraisal)
was undeclarable -- the cash-out flows in gross. Account transfers had no
fee-out / reimbursement / match-bonus modeling. The only existing members of
this class were a property sale's ``selling_costs`` and a mortgage's
``cash_back`` origination credit (#1075), each ad-hoc.

This module tests the general mechanism end-to-end (DP#11/DP#18: drive the
fold, assert the engine's observable output -- never hand-build engine
internals) and the pure arithmetic seam (DP#11: a unit test of a pure
function calling that function directly is correct):

* ``transaction_costs.validate_transaction_cost`` -- the loud-validation
  contract (DP#32): a negative amount, an unknown direction, a date before
  ``as_of``, and a missing date/anchor are each refused rather than
  producing a plausible wrong number.
* ``transaction_costs.year0_net_refinance_cost`` -- the signed NET year-0
  LUMP cost of a refinance origination (costs minus credits), applied as a
  year-0-equivalent reduction of the deployable principal (the SAME seam
  #137's deployment-lag carry uses -- reused, not reinvented).
* ``transaction_costs.installment_cash_flows`` -- an installment schedule
  aggregated into calendar years, tested against a year-0-lump variant to
  show a DIFFERENT trajectory (an installment is NOT a year-0 lump).
* The contract -> internal-config -> SimulationConfig -> engine seam (DP#18:
  the leaf reaches the engine, not just the merged config).
* Both directions (cost AND credit) tested through ``FamilySimulation.run``.
* Net-vs-gross surfacing on the year-0 YearResult.
* Absence of any ``transaction_costs`` declaration = byte-identical (the
  golden no-op, DP#32).

Fabricated round numbers, role-based names (DP#4/DP#15).
"""
import copy
import unittest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transaction_costs
from _example_doc import minimal_example
from contract_transaction_costs import map_transaction_costs
from transaction_costs import (
    installment_cash_flows, lump_cash_flow, validate_transaction_cost,
    year0_net_refinance_cost,
)

# ============================================================================
# Unit tests: the pure arithmetic + validation seam (DP#11).
# ============================================================================

AS_OF = "2026-06-30"


class ValidateTransactionCostTest(unittest.TestCase):
    """``validate_transaction_cost`` is the loud-validation contract (DP#32)."""

    def test_negative_amount_is_refused(self):
        """A negative amount is a bad input, not a sign (the `direction`
        field carries the sign). Refused loudly, naming the bad value."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": -500, "direction": "cost", "anchor": "year_0"},
                AS_OF)
        self.assertIn("amount=-500", str(cm.exception))
        self.assertIn("DP#32", str(cm.exception))

    def test_unknown_direction_is_refused(self):
        """An unknown direction is refused rather than silently coerced to
        the favourable value (DP#32 two-way)."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": 500, "direction": "rebate", "anchor": "year_0"},
                AS_OF)
        self.assertIn("'rebate'", str(cm.exception))

    def test_unknown_event_is_refused(self):
        """A new event is a real modelling decision, not a free string."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "vehicle_purchase",
                 "amount": 500, "direction": "cost", "anchor": "year_0"},
                AS_OF)
        self.assertIn("'vehicle_purchase'", str(cm.exception))

    def test_date_before_as_of_is_refused(self):
        """A date before the snapshot cannot be priced forward (the up-front
        lump is already paid); refused rather than silently dropped (DP#32)."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": 500, "direction": "cost", "date": "2025-01-01"},
                AS_OF)
        self.assertIn("before the document's as_of", str(cm.exception))

    def test_neither_date_nor_anchor_is_refused(self):
        """Exactly one of date/anchor is required; neither is a bad shape."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": 500, "direction": "cost"},
                AS_OF)
        self.assertIn("NEITHER", str(cm.exception))

    def test_both_date_and_anchor_is_refused(self):
        """Exactly one of date/anchor; both is a contradictory shape."""
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": 500, "direction": "cost",
                 "date": "2026-07-01", "anchor": "year_0"},
                AS_OF)
        self.assertIn("BOTH", str(cm.exception))

    def test_unknown_anchor_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            validate_transaction_cost(
                {"id": "x", "event": "refinance_origination",
                 "amount": 500, "direction": "cost", "anchor": "year_3"},
                AS_OF)
        self.assertIn("'year_3'", str(cm.exception))

    def test_valid_entry_does_not_raise(self):
        """A well-formed entry (each validated shape) passes."""
        validate_transaction_cost(
            {"id": "x", "event": "refinance_origination",
             "amount": 500, "direction": "cost", "anchor": "year_0"}, AS_OF)
        validate_transaction_cost(
            {"id": "y", "event": "account_transfer_in",
             "amount": 0, "direction": "credit", "date": "2026-09-01"}, AS_OF)

    def test_zero_amount_is_a_real_value(self):
        """A declared 0 is a real value (a $0 cost/credit), honoured
        explicitly -- not a fallback (DP#32)."""
        validate_transaction_cost(
            {"id": "z", "event": "refinance_origination",
             "amount": 0, "direction": "cost", "anchor": "year_0"}, AS_OF)


class Year0NetRefinanceCostTest(unittest.TestCase):
    """``year0_net_refinance_cost`` is a pure function (DP#3)."""

    def test_empty_is_zero(self):
        self.assertEqual(year0_net_refinance_cost([], AS_OF), 0.0)

    def test_no_refinance_entries_is_zero(self):
        """Only refinance_origination entries contribute to the year-0 lump;
        account_transfer_in entries do not."""
        entries = [
            {"id": "a", "event": "account_transfer_in", "amount": 1000,
             "direction": "cost", "anchor": "year_0"},
            {"id": "b", "event": "account_transfer_in", "amount": 2000,
             "direction": "credit", "anchor": "year_0"},
        ]
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 0.0)

    def test_costs_sum_to_a_net_cost(self):
        """Three itemized refinance origination costs (discharge 1200,
        notary 1800, title 600) net to +3600 (a net cost that reduces the
        deployable principal)."""
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1200,
             "direction": "cost", "anchor": "year_0", "label": "discharge"},
            {"id": "c2", "event": "refinance_origination", "amount": 1800,
             "direction": "cost", "anchor": "year_0", "label": "notary"},
            {"id": "c3", "event": "refinance_origination", "amount": 600,
             "direction": "cost", "anchor": "year_0", "label": "title"},
        ]
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 3600.0)

    def test_net_credit_is_floored_at_the_seam_not_negative(self):
        """Finding 1 (money conservation, DP#18): a credit larger than the
        costs yields a NEGATIVE raw net, but the deployable-seam value is
        FLOORED at zero so the deployable principal can never exceed the
        borrowed lump while the debt side stays at the full lump (a lender
        credit larger than the fees must not conjure invested principal from
        nowhere). The excess credit (2000) is routed as a year-0 SAVINGS cash
        flow by the adapter (see year0_refinance_excess_credit), not applied
        at the deployable seam."""
        from transaction_costs import year0_refinance_excess_credit
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "anchor": "year_0"},
            {"id": "cr1", "event": "refinance_origination", "amount": 3000,
             "direction": "credit", "anchor": "year_0"},
        ]
        # The deployable-seam net is FLOORED: never negative.
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 0.0)
        # The excess credit (raw net was -2000) is available for routing as
        # a year-0 savings cash flow.
        self.assertEqual(year0_refinance_excess_credit(entries, AS_OF), 2000.0)

    def test_excess_credit_is_zero_when_costs_exceed_or_offset(self):
        """The excess credit (for the savings cash flow) is 0.0 when costs
        exceed credits (a net cost) or exactly offset them."""
        from transaction_costs import year0_refinance_excess_credit
        # Net cost: no excess credit.
        cost_only = [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "anchor": "year_0"},
        ]
        self.assertEqual(year0_refinance_excess_credit(cost_only, AS_OF), 0.0)
        self.assertEqual(year0_net_refinance_cost(cost_only, AS_OF), 1000.0)
        # Costs exactly offset credits: no excess credit, seam 0.0.
        offset = [
            {"id": "c1", "event": "refinance_origination", "amount": 1500,
             "direction": "cost", "anchor": "year_0"},
            {"id": "cr1", "event": "refinance_origination", "amount": 1500,
             "direction": "credit", "anchor": "year_0"},
        ]
        self.assertEqual(year0_refinance_excess_credit(offset, AS_OF), 0.0)
        self.assertEqual(year0_net_refinance_cost(offset, AS_OF), 0.0)

    def test_costs_and_credit_offset_to_zero(self):
        """Costs exactly offset by a credit -> 0.0 (byte-identical)."""
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1500,
             "direction": "cost", "anchor": "year_0"},
            {"id": "cr1", "event": "refinance_origination", "amount": 1500,
             "direction": "credit", "anchor": "year_0"},
        ]
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 0.0)

    def test_installment_refinance_entry_is_excluded_from_year0_lump(self):
        """An installment is NOT a year-0 lump -- it is excluded from the
        deployable-principal reduction (it flows as dated cash flows)."""
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1200,
             "direction": "cost", "anchor": "year_0"},
            {"id": "ins", "event": "refinance_origination", "amount": 2400,
             "direction": "credit", "anchor": "year_0",
             "installments": {"months": 12}},
        ]
        # Only the 1200 cost lump counts; the 2400 installment is excluded.
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 1200.0)

    def test_non_year0_dated_refinance_lump_is_excluded(self):
        """A refinance_origination dated to a later year is NOT a year-0
        lump (the engine wires only the year-0 attachment point in this
        layer); it flows as a dated cash flow via lump_cash_flow."""
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "date": "2028-01-01"},
        ]
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 0.0)

    def test_year0_dated_refinance_lump_is_included(self):
        """A refinance_origination with an explicit date in the start year
        IS a year-0 lump."""
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "date": "2026-09-01"},
        ]
        self.assertEqual(year0_net_refinance_cost(entries, AS_OF), 1000.0)

    def test_bad_entry_refuses_loudly(self):
        """A single bad entry refuses the whole computation (DP#32)."""
        with self.assertRaises(ValueError):
            year0_net_refinance_cost(
                [{"id": "c1", "event": "refinance_origination", "amount": -1,
                  "direction": "cost", "anchor": "year_0"}], AS_OF)


class InstallmentCashFlowsTest(unittest.TestCase):
    """``installment_cash_flows`` aggregates N monthly payments into
    calendar years (DP#3)."""

    def test_12_months_starting_mid_year_spans_two_years(self):
        """A 12-month schedule starting 2026-06-30: 7 months in 2026
        (Jun-Dec), 5 months in 2027 (Jan-May). $2400 over 12 months =
        $200/month. The schedule spans two calendar years."""
        entry = {"id": "b", "event": "account_transfer_in", "amount": 2400,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 12}}
        flows = installment_cash_flows(entry, AS_OF)
        years = {f["year"] for f in flows}
        self.assertEqual(years, {2026, 2027})
        total = sum(f["amount"] for f in flows)
        self.assertAlmostEqual(total, 2400.0, places=6)

    def test_18_months_spans_two_years(self):
        """An 18-month schedule starting mid-2026 spans 2026 and 2027 (Jun
        2026 - Nov 2027)."""
        entry = {"id": "b", "event": "account_transfer_in", "amount": 1800,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 18}}
        flows = installment_cash_flows(entry, AS_OF)
        years = {f["year"] for f in flows}
        self.assertEqual(years, {2026, 2027})
        self.assertAlmostEqual(sum(f["amount"] for f in flows), 1800.0, places=6)

    def test_30_months_spans_three_years(self):
        """A 30-month schedule starting mid-2026 spans 2026, 2027, 2028 (Jun
        2026 - Nov 2028)."""
        entry = {"id": "b", "event": "account_transfer_in", "amount": 3000,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 30}}
        flows = installment_cash_flows(entry, AS_OF)
        years = {f["year"] for f in flows}
        self.assertEqual(years, {2026, 2027, 2028})
        self.assertAlmostEqual(sum(f["amount"] for f in flows), 3000.0, places=6)

    def test_cost_direction_is_negative(self):
        """A cost installment is a negative outflow (the cash-flow sign)."""
        entry = {"id": "c", "event": "account_transfer_in", "amount": 1200,
                 "direction": "cost", "anchor": "year_0",
                 "installments": {"months": 6}}
        flows = installment_cash_flows(entry, AS_OF)
        self.assertTrue(all(f["amount"] < 0 for f in flows))
        self.assertAlmostEqual(sum(f["amount"] for f in flows), -1200.0, places=6)

    def test_credit_direction_is_positive(self):
        entry = {"id": "c", "event": "account_transfer_in", "amount": 1200,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 6}}
        flows = installment_cash_flows(entry, AS_OF)
        self.assertTrue(all(f["amount"] > 0 for f in flows))

    def test_explicit_start_date_phases_from_that_date(self):
        """A `date` entry phases from that date, not as_of. 18 months from
        2027-01-15 spans 2027 and 2028 (Jan 2027 - Jun 2028)."""
        entry = {"id": "c", "event": "account_transfer_in", "amount": 1800,
                 "direction": "credit", "date": "2027-01-15",
                 "installments": {"months": 18}}
        flows = installment_cash_flows(entry, AS_OF)
        years = {f["year"] for f in flows}
        self.assertEqual(years, {2027, 2028})

    def test_zero_months_returns_empty(self):
        """Defensive: a 0-month schedule produces no payments (the schema
        forbids this, but the function does not divide by zero)."""
        entry = {"id": "c", "event": "account_transfer_in", "amount": 1200,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 0}}
        self.assertEqual(installment_cash_flows(entry, AS_OF), [])

    def test_non_taxable_is_the_default_treatment(self):
        """Finding 7: the default tax_treatment (absent key) is non_taxable
        -- a cost/credit is a rebate unless declared a taxable bonus
        (DP#13: a default is a fallback for absent input). The internal
        cash-flow value is the hyphenated 'non-taxable' the engine reads."""
        entry = {"id": "c", "event": "account_transfer_in", "amount": 600,
                 "direction": "credit", "anchor": "year_0",
                 "installments": {"months": 3}}
        for f in installment_cash_flows(entry, AS_OF):
            self.assertEqual(f["tax_treatment"], "non-taxable")

    def test_post_tax_bonus_flows_differently_from_non_taxable_rebate(self):
        """Finding 7: the declarable tax_treatment field is plumbed into
        installment_cash_flows (replacing the hardcoded value). A post_tax
        bonus (a taxable match bonus) is MARKED 'post-tax' -- distinct from a
        non_taxable rebate ('non-taxable') -- so the engine's cash-flow
        mechanism can apply the rebate-vs-bonus distinction (DP#27). The two
        SAME-amount credits thus flow with DIFFERENT tax_treatment markings
        (the rebate-vs-bonus distinction is now real in the data, not a
        hardcoded guess)."""
        rebate = {"id": "r", "event": "account_transfer_in", "amount": 600,
                  "direction": "credit", "anchor": "year_0",
                  "tax_treatment": "non_taxable",
                  "installments": {"months": 3}}
        bonus = {"id": "b", "event": "account_transfer_in", "amount": 600,
                 "direction": "credit", "anchor": "year_0",
                 "tax_treatment": "post_tax",
                 "installments": {"months": 3}}
        rebate_flows = installment_cash_flows(rebate, AS_OF)
        bonus_flows = installment_cash_flows(bonus, AS_OF)
        # Same amounts, same years, DIFFERENT tax_treatment marking.
        self.assertEqual([f["amount"] for f in rebate_flows],
                         [f["amount"] for f in bonus_flows])
        self.assertEqual([f["year"] for f in rebate_flows],
                         [f["year"] for f in bonus_flows])
        self.assertEqual([f["tax_treatment"] for f in rebate_flows],
                         ["non-taxable"] * len(rebate_flows))
        self.assertEqual([f["tax_treatment"] for f in bonus_flows],
                         ["post-tax"] * len(bonus_flows))
        self.assertNotEqual(
            {f["tax_treatment"] for f in rebate_flows},
            {f["tax_treatment"] for f in bonus_flows})


class LumpCashFlowTest(unittest.TestCase):
    """``lump_cash_flow`` for a non-year-0-lump entry (DP#3)."""

    def test_account_transfer_in_cost_is_negative_year0_flow(self):
        entry = {"id": "f", "event": "account_transfer_in", "amount": 150,
                 "direction": "cost", "anchor": "year_0"}
        cf = lump_cash_flow(entry, AS_OF)
        self.assertEqual(cf["year"], 2026)
        self.assertEqual(cf["amount"], -150.0)
        self.assertEqual(cf["tax_treatment"], "non-taxable")

    def test_account_transfer_in_credit_is_positive_year0_flow(self):
        entry = {"id": "r", "event": "account_transfer_in", "amount": 150,
                 "direction": "credit", "anchor": "year_0"}
        cf = lump_cash_flow(entry, AS_OF)
        self.assertEqual(cf["amount"], 150.0)

    def test_post_tax_bonus_lump_is_marked_taxable(self):
        """Finding 7: a post_tax bonus lump is MARKED 'post-tax' (a taxable
        bonus), distinct from the default non_taxable rebate ('non-taxable')
        -- the declarable tax_treatment is plumbed into lump_cash_flow
        instead of the hardcoded value (DP#27)."""
        bonus = {"id": "b", "event": "account_transfer_in", "amount": 500,
                 "direction": "credit", "anchor": "year_0",
                 "tax_treatment": "post_tax"}
        rebate = {"id": "r", "event": "account_transfer_in", "amount": 500,
                  "direction": "credit", "anchor": "year_0",
                  "tax_treatment": "non_taxable"}
        self.assertEqual(lump_cash_flow(bonus, AS_OF)["tax_treatment"],
                         "post-tax")
        self.assertEqual(lump_cash_flow(rebate, AS_OF)["tax_treatment"],
                         "non-taxable")
        # Same amount/year, DIFFERENT tax_treatment marking.
        self.assertEqual(lump_cash_flow(bonus, AS_OF)["amount"],
                         lump_cash_flow(rebate, AS_OF)["amount"])


class MapTransactionCostsTest(unittest.TestCase):
    """The adapter: contract -> internal config (DP#18)."""

    def test_empty_returns_zero_and_no_cash_flows(self):
        net, flows = map_transaction_costs({}, AS_OF)
        self.assertEqual(net, 0.0)
        self.assertEqual(flows, [])

    def test_refinance_lumps_produce_year0_net_only(self):
        """Year-0 refinance lumps net into the year0 cost; NO cash flows
        (they go through the deployable seam, not cash_flows)."""
        doc = {"transaction_costs": [
            {"id": "c1", "event": "refinance_origination", "amount": 1200,
             "direction": "cost", "anchor": "year_0"},
            {"id": "c2", "event": "refinance_origination", "amount": 600,
             "direction": "cost", "anchor": "year_0"},
        ]}
        net, flows = map_transaction_costs(doc, AS_OF)
        self.assertEqual(net, 1800.0)
        self.assertEqual(flows, [])

    def test_installments_produce_cash_flows_not_year0_net(self):
        """An installment does NOT contribute to the year0 net (it is not a
        year-0 lump); it produces dated cash flows."""
        doc = {"transaction_costs": [
            {"id": "b", "event": "account_transfer_in", "amount": 1200,
             "direction": "credit", "anchor": "year_0",
             "installments": {"months": 12}},
        ]}
        net, flows = map_transaction_costs(doc, AS_OF)
        self.assertEqual(net, 0.0)
        self.assertEqual(len(flows), 2)  # spans 2026 + 2027

    def test_account_transfer_in_lumps_produce_cash_flows(self):
        """A non-installment account_transfer_in (fee-out/reimbursement) flows
        as a year-0 cash flow, NOT the deployable seam."""
        doc = {"transaction_costs": [
            {"id": "f", "event": "account_transfer_in", "amount": 150,
             "direction": "cost", "anchor": "year_0"},
            {"id": "r", "event": "account_transfer_in", "amount": 150,
             "direction": "credit", "anchor": "year_0"},
        ]}
        net, flows = map_transaction_costs(doc, AS_OF)
        self.assertEqual(net, 0.0)
        # Two year-0 cash flows (fee-out -150, reimbursement +150).
        self.assertEqual(len(flows), 2)
        self.assertAlmostEqual(sum(f["amount"] for f in flows), 0.0)

    def test_bad_entry_refuses_loudly(self):
        """A single bad entry refuses the whole block (DP#32)."""
        doc = {"transaction_costs": [
            {"id": "c1", "event": "refinance_origination", "amount": -1,
             "direction": "cost", "anchor": "year_0"},
        ]}
        with self.assertRaises(ValueError):
            map_transaction_costs(doc, AS_OF)

    def test_dated_refinance_lump_outside_year0_routes_as_dated_flow(self):
        """Finding 3: a refinance_origination LUMP dated to a LATER year
        (not the projection's first calendar year) is NOT a year-0 deployable
        lump (the engine wires only the year-0 attachment point in this
        layer) -- it is excluded from the year0 net AND routed as a single
        dated cash flow for that year. The routing decision (year-0 seam vs
        dated flow) is currently untested; this pins it."""
        doc = {"transaction_costs": [
            {"id": "later_refi", "event": "refinance_origination",
             "amount": 800, "direction": "cost", "date": "2028-04-01"},
        ]}
        net, flows = map_transaction_costs(doc, AS_OF)
        # NOT a year-0 lump: the deployable-seam net is 0.0.
        self.assertEqual(net, 0.0)
        # Routed as a single dated cash flow for 2028 (a cost is a negative
        # outflow; non-taxable, a cost is never income).
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["year"], 2028)
        self.assertAlmostEqual(flows[0]["amount"], -800.0)
        self.assertEqual(flows[0]["tax_treatment"], "non-taxable")

    def test_net_credit_routes_excess_as_year0_savings_flow(self):
        """Finding 1 (adapter half): a net CREDIT among year-0 refinance lumps
        is FLOORED at the deployable seam (net=0.0) and the excess credit is
        routed as a year-0 SAVINGS cash flow (non-taxable -- a lender credit
        is a rebate, not income)."""
        doc = {"transaction_costs": [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "anchor": "year_0"},
            {"id": "cr1", "event": "refinance_origination", "amount": 4000,
             "direction": "credit", "anchor": "year_0"},
        ]}
        net, flows = map_transaction_costs(doc, AS_OF)
        self.assertEqual(net, 0.0)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["year"], 2026)
        self.assertAlmostEqual(flows[0]["amount"], 3000.0)
        self.assertEqual(flows[0]["tax_treatment"], "non-taxable")

    def test_unknown_tax_treatment_is_refused(self):
        """Finding 7: an unknown tax_treatment is refused rather than
        silently coerced to the default (DP#32 two-way)."""
        doc = {"transaction_costs": [
            {"id": "b", "event": "account_transfer_in", "amount": 100,
             "direction": "credit", "anchor": "year_0",
             "tax_treatment": "taxable"},
        ]}
        with self.assertRaises(ValueError) as cm:
            map_transaction_costs(doc, AS_OF)
        self.assertIn("tax_treatment='taxable'", str(cm.exception))


# ============================================================================
# Integration tests: drive FamilySimulation.run() and assert the engine's
# observable output (DP#11/DP#18). Reuses the deployment_lag test's fixture
# shape (a fabricated household with a mortgage + undrawn HELOC room, a
# cash-out refinance overlay).
# ============================================================================

from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation

TXN_START_YEAR = 2026
TXN_PRIMARY_BIRTH = 1980
TXN_SPOUSE_BIRTH = 1982
TXN_GROSS_RETURN = 0.06
TXN_OPENING_MORTGAGE = 200_000
TXN_OPENING_MARGIN = 150_000
TXN_HOUSE_VALUE = 600_000
TXN_MORTGAGE_RATE = 0.05
TXN_CASH_OUT = 100_000
TXN_AMORTIZATION = 25


def _txn_base_config() -> dict:
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': TXN_PRIMARY_BIRTH,
             'gross_income': 150_000, 'retirement_age': 65,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'birth_year': TXN_SPOUSE_BIRTH,
             'gross_income': 60_000, 'retirement_age': 65,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
        ]},
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': TXN_START_YEAR, 'projection_years': 10,
            'investment_return': TXN_GROSS_RETURN, 'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': TXN_HOUSE_VALUE,
            'mortgage_balance': TXN_OPENING_MORTGAGE,
            'mortgage_rate': TXN_MORTGAGE_RATE, 'amortization_years': 20,
            'margin_available': TXN_OPENING_MARGIN, 'ltv_max': 0.80,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
    }


def _run(transaction_cost_year0=0.0, time_step='yearly',
         cash_flows=None):
    """Drive the engine end-to-end: apply_overlay books the cash-out
    refinance, the net year-0 transaction cost is set on the property dict
    (the from_dict path the adapter writes), and FamilySimulation.run()
    folds the projection. ``cash_flows`` (optional) extends the config's
    cash_flows (the installment path). Returns the list of YearResult."""
    base_cfg = _txn_base_config()
    overlay = ScenarioOverlay(
        label='txn_test', cash_out=TXN_CASH_OUT,
        mortgage_rate=base_cfg['property']['mortgage_rate'],
        refinance_amortization_years=TXN_AMORTIZATION)
    overlaid_cfg = apply_overlay(base_cfg, overlay)
    if transaction_cost_year0 != 0.0:
        overlaid_cfg['property']['transaction_cost_year0'] = transaction_cost_year0
    if cash_flows:
        overlaid_cfg['cash_flows'] = (
            overlaid_cfg.get('cash_flows', []) + list(cash_flows))
    overlaid_cfg['assumptions']['time_step'] = time_step
    sim_cfg = SimulationConfig.from_dict(overlaid_cfg)
    lump_sum = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                          use_readvanceable=False, deduct_later=False,
                          lump_sum=lump_sum)
    return sim.run()


class TransactionCostIntegrationTest(unittest.TestCase):
    """Drive FamilySimulation.run() and assert the engine's observable
    output (DP#11/DP#18)."""

    def test_no_declared_cost_is_byte_identical(self):
        """A run with no declared transaction cost (0.0) surfaces
        transaction_cost_year0 = 0.0 on the year-0 result and is
        byte-identical to a config that never carried the key (DP#32)."""
        results = _run(transaction_cost_year0=0.0)
        self.assertEqual(results[0].transaction_cost_year0, 0.0)
        # Byte-identical: a config that never set the key runs the same.
        results_no_key = _run()
        self.assertEqual(results[-1].total_assets, results_no_key[-1].total_assets)

    def test_net_cost_surfaces_on_year_0_and_reduces_terminal(self):
        """A net year-0 refinance origination cost surfaces on the year-0
        result and REDUCES terminal assets (the deployable principal is
        smaller, so less compounds). Both sides of the cost (DP#17)."""
        no_cost = _run(transaction_cost_year0=0.0)
        with_cost = _run(transaction_cost_year0=5_000.0)
        self.assertAlmostEqual(with_cost[0].transaction_cost_year0, 5_000.0)
        # The cost is a YEAR-0 fact: 0.0 on every year after year 0.
        for r in with_cost[1:]:
            self.assertEqual(r.transaction_cost_year0, 0.0)
        # Terminal assets are LOWER with the cost (the cost is real, not $0).
        self.assertGreater(no_cost[-1].total_assets, with_cost[-1].total_assets)

    def test_net_credit_does_not_inflate_deployable_and_routes_as_savings(self):
        """Finding 1 (BLOCKER, money conservation, DP#18): a net year-0 CREDIT
        among refinance_origination lumps (credits exceed costs) must NOT
        inflate the deployable principal above the borrowed lump while the
        debt side stays at the full lump. The deployable-seam net is FLOORED
        at zero, so the borrowed/debt side is unchanged and the invested
        principal never exceeds the borrowed lump; the excess credit is
        routed instead as a year-0 SAVINGS cash flow (a lender credit larger
        than the fees arrives as savings the household can deploy, not as free
        investable principal booked against no debt)."""
        # A net CREDIT: 1000 cost + 6000 credit = raw net -5000 (a 5000 excess
        # credit). Routed through the adapter so both the floored seam and the
        # savings cash flow are exercised (the production path).
        entries = [
            {"id": "c1", "event": "refinance_origination", "amount": 1000,
             "direction": "cost", "anchor": "year_0"},
            {"id": "cr1", "event": "refinance_origination", "amount": 6000,
             "direction": "credit", "anchor": "year_0"},
        ]
        net, flows = map_transaction_costs(
            {"transaction_costs": entries}, AS_OF)
        # The deployable-seam net is FLOORED to 0.0 (no deployable inflation).
        self.assertEqual(net, 0.0)
        # The 5000 excess credit arrives as a year-0 savings cash flow.
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["year"], TXN_START_YEAR)
        self.assertAlmostEqual(flows[0]["amount"], 5000.0)
        self.assertEqual(flows[0]["tax_treatment"], "non-taxable")
        # Run the engine through the adapter path (floored seam + savings flow).
        no_credit = _run(transaction_cost_year0=0.0)
        with_credit = _run(transaction_cost_year0=net, cash_flows=flows)
        # The borrowed/debt side is UNCHANGED: the year-0 mortgage balance is
        # the same with and without the net credit (the seam was floored).
        self.assertAlmostEqual(no_credit[0].mortgage_balance,
                              with_credit[0].mortgage_balance, places=2)
        # The deployable seam surfaces 0.0 (floored), NOT -5000.
        self.assertAlmostEqual(with_credit[0].transaction_cost_year0, 0.0)
        # The invested principal never exceeds the borrowed lump. The year-0
        # invested total (RRSP + TFSA + non-reg balance) is <= the borrowed
        # lump_sum: the floored seam deploys no more than the borrowed lump,
        # and the excess credit arrives as SAVINGS (a real cash inflow), not
        # as inflated borrow booked against no debt (DP#18 money conservation).
        invested0 = (with_credit[0].total_rrsp + with_credit[0].total_tfsa
                     + with_credit[0].non_reg_balance)
        self.assertLessEqual(invested0, TXN_CASH_OUT + TXN_OPENING_MARGIN + 1.0)
        # The excess credit arrives as savings: terminal assets are HIGHER
        # than the no-credit run (the savings flow compounds), but the gain
        # comes from SAVINGS, not from inflated deployable principal.
        self.assertGreater(with_credit[-1].total_assets,
                           no_credit[-1].total_assets)

    def test_net_vs_gross_gap_is_visible_on_year_0(self):
        """The gross cash-out advance is the year-0 lump_sum (margin + cash
        out); the net deployable is lump_sum - transaction_cost_year0. The
        year-0 result surfaces the cost so the gross-vs-net gap is visible
        (DP#32). A larger cost surfaces a larger gap and deploys less."""
        small = _run(transaction_cost_year0=2_000.0)
        large = _run(transaction_cost_year0=8_000.0)
        self.assertGreater(large[0].transaction_cost_year0,
                           small[0].transaction_cost_year0)
        # The larger cost deploys LESS year-0 principal (less compounds).
        self.assertLess(large[-1].total_assets, small[-1].total_assets)

    def test_debt_side_unchanged_by_transaction_cost(self):
        """The full borrowed lump stays on the debt side (the year-0 purpose
        tracing is untouched): the year-0 mortgage balance is the same with
        and without a declared transaction cost. The cost reduces only the
        DEPLOYED principal, not the debt booked (money conservation, DP#18)."""
        no_cost = _run(transaction_cost_year0=0.0)
        with_cost = _run(transaction_cost_year0=5_000.0)
        self.assertAlmostEqual(no_cost[0].mortgage_balance,
                              with_cost[0].mortgage_balance, places=2)

    def test_monthly_path_surfaces_year_0_cost(self):
        """The monthly fold surfaces the year-0 net cost on results[0],
        mirroring the yearly path (DP#9: one spelling of the year-0 fact)."""
        yearly = _run(transaction_cost_year0=5_000.0, time_step='yearly')
        monthly = _run(transaction_cost_year0=5_000.0, time_step='monthly')
        self.assertAlmostEqual(yearly[0].transaction_cost_year0,
                              monthly[0].transaction_cost_year0, places=6)
        for r in monthly[1:]:
            self.assertEqual(r.transaction_cost_year0, 0.0)

    def test_installment_vs_lump_produce_different_trajectories(self):
        """Finding 2: an installment is NOT a year-0 lump. A $2400 COST paid
        as a year-0 deployable-seam lump (reducing the deployable principal
        so NET proceeds are what deploys) projects to a DIFFERENT trajectory
        than the SAME $2400 cost paid as a 12-month installment via cash
        flows (spanning 2026 + 2027). The two are now economically
        comparable: both are real $2400 costs (the prior version compared a
        net CREDIT lump that conjured free money -- fixed by Finding 1's
        deployable-seam floor). The lump removes $2400 of deployable
        principal at year 0 (a full year-0 opportunity cost: that $2400 never
        compounds), so it ends LOWER than the installment, which phases the
        cost across two calendar years (less removed from compounding early)."""
        # Lump: a $2400 net COST at year 0 through the deployable seam
        # (reduces the deployable principal; the full borrowed lump stays on
        # the debt side -- DP#18 money conservation).
        lump = _run(transaction_cost_year0=2_400.0)
        # Installment: the SAME $2400 cost paid over 12 months starting
        # mid-2026 as cash flows (7 months in 2026 (Jun-Dec) + 5 months in
        # 2027 (Jan-May) -- the adapter's calendar-year aggregation).
        installment_flows = transaction_costs.installment_cash_flows(
            {"id": "b", "event": "account_transfer_in", "amount": 2400,
             "direction": "cost", "anchor": "year_0",
             "installments": {"months": 12}}, "2026-06-30")
        # A 12-month schedule from mid-2026 spans 2026 + 2027 (7 + 5 months).
        years = {f["year"] for f in installment_flows}
        self.assertEqual(years, {2026, 2027})
        installment = _run(transaction_cost_year0=0.0,
                           cash_flows=installment_flows)
        # The two trajectories differ -- the installment does NOT remove the
        # full $2400 from year-0 compounding the way the lump does.
        self.assertNotEqual(
            round(lump[-1].total_assets, 6),
            round(installment[-1].total_assets, 6))
        # The lump (a full year-0 deployable reduction) ends LOWER than the
        # installment (which phases the cost across two years), because the
        # lump's $2400 never compounds from year 0 while the installment's
        # later payments still earn a partial year of growth first.
        self.assertLess(lump[-1].total_assets, installment[-1].total_assets)

    def test_account_transfer_in_fee_and_reimbursement_via_cash_flows(self):
        """A fee-out cost and a reimbursement credit (account_transfer_in
        lumps) flow as year-0 cash flows: a $150 fee (negative) and a $150
        reimbursement (positive) net to zero on the year, so the trajectory
        is ~unchanged vs no transaction costs (money conservation: the two
        cancel). Verifies the account_transfer_in attachment point flows
        through the cash_flows mechanism."""
        flows = [
            {"year": 2026, "amount": -150.0, "tax_treatment": "non-taxable"},
            {"year": 2026, "amount": 150.0, "tax_treatment": "non-taxable"},
        ]
        baseline = _run(transaction_cost_year0=0.0)
        with_flows = _run(transaction_cost_year0=0.0, cash_flows=flows)
        # The fee and reimbursement cancel -> trajectory ~unchanged.
        self.assertAlmostEqual(baseline[-1].total_assets,
                               with_flows[-1].total_assets, places=2)


# ============================================================================
# Contract-mapping test: the adapter (contract_transaction_costs
# .map_transaction_costs) reads the declared transaction_costs[] and maps
# them onto the internal property key + cash_flows -- the schema-coverage-
# relevant hop (the leaf is consumed, not merely parsed).
# ============================================================================

import input_contract as ic  # noqa: E402
from contract_schema import validate_contract  # noqa: E402


def _example_doc_with_transaction_costs() -> dict:
    """Load a MINIMAL (illustrative-block-free) example and re-add the shipped
    example's declared transaction_costs block -- the schema-coverage
    illustration this contract-mapping test exercises end-to-end.

    minimal_example() (tests/_example_doc.py) strips EVERY optional
    illustrative block (transaction_costs, deployment_lag_months,
    parking_rate, advance_split) in ONE place so a future feature's
    illustration cannot confound this test; this function adds back ONLY the
    block it tests (the example's declared transaction_costs), so the
    contract-mapping assertions pin transaction_costs alone, not
    transaction_costs-plus-an-unrelated-illustration."""
    doc = minimal_example()
    # Re-add the shipped example's declared transaction_costs block (the
    # illustration under test), deep-copied so the shared example on disk and
    # other tests' copies are untouched.
    from test_input_contract import _load_example
    doc["transaction_costs"] = copy.deepcopy(
        _load_example().get("transaction_costs", []))
    return doc


class TransactionCostContractMappingTest(unittest.TestCase):
    """The contract leaf -> internal config -> SimulationConfig seam (DP#18)."""

    def test_contract_maps_to_year0_net_and_cash_flows(self):
        """A contract declaring transaction_costs maps to the internal
        property.transaction_cost_year0 key (the deployable seam) and
        appends installment/account_transfer_in cash flows."""
        doc = _example_doc_with_transaction_costs()
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        # The example's three refinance origination costs (1200+1800+600)
        # net to +3600 (a net cost).
        self.assertEqual(legacy["property"]["transaction_cost_year0"], 3600.0)
        # The account_transfer_in fee/reimbursement/installment cash flows
        # are appended to the internal cash_flows list.
        cf_years = [cf["year"] for cf in legacy["cash_flows"]]
        # The 12-month match bonus (2400 over 12 months from 2026-06-30)
        # spans 2026 and 2027.
        self.assertIn(2027, cf_years)

    def test_contract_with_no_transaction_costs_maps_no_key(self):
        """A contract with no transaction_costs maps NEITHER the year0 key
        NOR extra cash flows -- byte-identical (DP#24/DP#32)."""
        # minimal_example() strips transaction_costs (and every other
        # illustrative block) in one place, so this is the true no-txn-cost
        # contract without a hand-strip.
        doc = minimal_example()
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("transaction_cost_year0", legacy["property"])

    def test_round_trip_preserves_a_declared_cost(self):
        """DP#24: a declared net year-0 cost survives a load->modify->save
        cycle. to_dict re-emits it (only when non-zero); from_dict reads it
        back. A no-cost config round-trips to absence."""
        doc = _example_doc_with_transaction_costs()
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(sim_cfg.transaction_cost_year0, 3600.0)
        round_tripped = sim_cfg.to_dict()
        self.assertEqual(round_tripped["property"]["transaction_cost_year0"],
                         3600.0)
        # A no-cost config round-trips to absence. minimal_example() strips
        # transaction_costs in one place, so this is the true no-txn-cost
        # contract without a hand-strip.
        no_doc = minimal_example()
        no_cfg = SimulationConfig.from_dict(ic.to_internal_config(no_doc))
        self.assertNotIn("transaction_cost_year0", no_cfg.to_dict()["property"])

    def test_loud_refusals_at_the_contract_boundary(self):
        """A contract declaring a bad transaction_costs entry is refused at
        validate_contract (schema) -- and the adapter re-validates too."""
        doc = _example_doc_with_transaction_costs()
        doc["transaction_costs"].append(
            {"id": "bad", "event": "refinance_origination", "amount": -1,
             "direction": "cost", "anchor": "year_0"})
        # Schema validation refuses the negative amount (money minimum 0).
        with self.assertRaises(Exception):
            validate_contract(doc)


if __name__ == "__main__":
    unittest.main()
