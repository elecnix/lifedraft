#!/usr/bin/env python3
"""Issue #1075 (data-model half): the multi-tranche readvanceable mortgage.

A household's readvanceable all-in-one product is ONE registered charge split
into N fixed sub-accounts: a house mortgage (>= $600k for a $1,200 cash-back),
a deductible investment mortgage, and a readvanceable line (the HELOC). The
faithful contract encoding is N ``kind=mortgage``
liabilities sharing one ``collateral`` -- which #652 refused loudly because
the adapter consumed exactly one mortgage facility.

#1075's data-model half makes the adapter consume N:

  - balances SUM into the single downstream ``mortgage_balance``;
  - the rate is the balance-weighted average (rate is linear in balance, so
    the sum's interest is EXACTLY what the tranches charge separately --
    nothing invented, nothing lost);
  - each tranche's new optional ``deductible`` flag (interest deductible
    under ITA s.20(1)(c): borrowed for an income-producing non-registered
    investment) accumulates into ``property.deductible_mortgage_balance``
    AND ``property.deductible_mortgage_interest`` -- the balance and its
    EXACT annual interest (each flagged tranche's balance * its OWN rate,
    NOT the weighted mortgage_rate times the summed balance, which equals
    the own-rate sum only when every tranche shares one rate) that the
    #850 pricing consumes;
  - each tranche's new optional ``cash_back`` block (a lender cash-back paid
    at origination, clawed back pro-rata if fully prepaid before
    ``term_years``) is credited as an ``origination_cash_back`` cash-flow in
    the projection's first year -- the clawback itself is contingent on an
    early full prepayment the year-0 engine cannot produce (fixed
    amortization schedule), so it is recorded as data, not priced yet.

DP#32 holds on both sides: a plain single-mortgage contract with none of the
new flags is byte-identical to pre-#1075, and a multi-mortgage document that
CANNOT be one charge (different collaterals / different amortization terms /
a zero total) is refused loudly rather than silently averaged.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based. No real household's data appears here.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_dp_income_scenario_reaches_engine import _two_generation_subset

# The example's principal: house 650,000 -> 80% charge = 520,000; the
# baseline mortgage (340,000) + HELOC limit (150,000) leave 30,000 of charge
# headroom, so every added tranche in this file stays <= 30,000 (a larger sum
# trips the #664/#689 charge refusal -- tested explicitly below).
CHARGE_HEADROOM = 30_000


def _load_doc():
    with open(ic.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _mortgage(doc):
    return next(l for l in doc["liabilities"] if l["kind"] == "mortgage")


def _tranche(template, liab_id, balance, rate, **overrides):
    """A second kind=mortgage tranche against the same charge, from the
    example's own mortgage template (DP#9: one spelling of the schema
    shape). The new per-tranche flags (``deductible``/``cash_back``) are
    NOT inherited from the template -- each test declares them explicitly
    via overrides, so a template that carries them cannot leak them into
    an unintended tranche."""
    t = copy.deepcopy(template)
    t["id"] = liab_id
    t["balance"] = {"amount": balance, "as_of": template["balance"]["as_of"]}
    t["rate"] = rate
    t.pop("deductible", None)
    t.pop("cash_back", None)
    for key, value in overrides.items():
        t[key] = value
    return t


# ============================================================================
# (a) Two tranches sharing one charge: sum + balance-weighted rate
# ============================================================================

class TestTwoTranchesSum(unittest.TestCase):
    def test_balances_sum_and_rate_is_balance_weighted(self):
        """The #1075 core: 340,000 @ 4.5% (house tranche) + 30,000 @ 6.5%
        (investment tranche) on the SAME charge load as ONE facility --
        mortgage_balance 370,000, mortgage_rate (340,000*0.045 +
        30,000*0.065) / 370,000. The weighted average is exact: the summed
        facility's year-0 interest (balance * rate) equals the tranches'
        separate interest, so no interest is invented or lost (DP#18)."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "investment_tranche", 30_000, 0.065)]
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["mortgage_balance"], 370_000)
        expected = (340_000 * 0.045 + 30_000 * 0.065) / 370_000
        self.assertAlmostEqual(cfg["property"]["mortgage_rate"], expected)
        # Interest preserved exactly by construction.
        self.assertAlmostEqual(
            cfg["property"]["mortgage_balance"] * cfg["property"]["mortgage_rate"],
            340_000 * 0.045 + 30_000 * 0.065)

    def test_amortization_years_are_shared(self):
        """Both tranches amortize on the same schedule (the engine has one);
        the shared declared term is carried as before."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "investment_tranche", 20_000, 0.05)]
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["amortization_years"],
                         _mortgage(doc)["amortization"]["years"])

    def test_the_aggregated_facility_feeds_the_engine(self):
        """The summed facility is not a config-only fiction: it drives the
        real amortization schedule (SimulationConfig -> FamilySimulation),
        exactly like a single mortgage's balance would -- the summed 370,000
        opens the schedule and erodes with each year's principal."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "investment_tranche", 30_000, 0.065)]
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(sim_cfg.mortgage_balance, 370_000)
        sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg))
        results = sim.run()
        # The summed balance amortizes from its full opening value...
        self.assertGreater(results[0].mortgage_balance, 0)
        self.assertLess(results[0].mortgage_balance, 370_000)
        # ...and keeps eroding year over year (a single schedule, driven by
        # the weighted rate and the shared term).
        balances = [r.mortgage_balance for r in results if r.mortgage_balance > 0]
        for earlier, later in zip(balances, balances[1:]):
            self.assertLess(later, earlier)


# ============================================================================
# (b) The deductible flag is carried and drives the s.20(1)(c) interest base
# ============================================================================

class TestDeductibleTranche(unittest.TestCase):
    def test_deductible_balance_sums_only_the_flagged_tranches(self):
        """340,000 non-deductible + 10,000 deductible + 15,000 deductible
        -> deductible_mortgage_balance 25,000 (never the whole 365,000; the
        sums stay inside the example's 520,000 charge), and the EXACT
        deductible interest is the flagged tranches' OWN-rate sum
        (10,000*0.065 + 15,000*0.06 = 1,550) -- not 25,000 times the
        weighted mortgage_rate, which blends the non-deductible house
        tranche's 4.5% in and understates the s.20(1)(c) deduction."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "invest_a", 10_000, 0.065, deductible=True),
            _tranche(_mortgage(doc), "invest_b", 15_000, 0.06, deductible=True),
        ]
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["deductible_mortgage_balance"], 25_000)
        self.assertEqual(cfg["property"]["deductible_mortgage_interest"],
                         10_000 * 0.065 + 15_000 * 0.06)
        # The counter-example: the weighted-rate product is NOT the true
        # deductible interest (it blends the 340,000 @ 4.5% house tranche).
        self.assertNotAlmostEqual(
            cfg["property"]["deductible_mortgage_interest"],
            cfg["property"]["deductible_mortgage_balance"]
            * cfg["property"]["mortgage_rate"])
        # The non-deductible tranche's balance is never folded into the
        # deductible figure, and the summed mortgage carries the whole debt.
        self.assertEqual(cfg["property"]["mortgage_balance"], 340_000 + 25_000)

    def test_deductible_flag_on_a_single_mortgage_is_carried_too(self):
        """The flag is per-LIABILITY, not per-multi-tranche: a plain contract
        with ONE mortgage and `deductible: true` surfaces the same keys, and
        with one tranche the exact interest IS balance * the (sole) rate."""
        doc = _load_doc()
        _mortgage(doc)["deductible"] = True
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["deductible_mortgage_balance"], 340_000)
        self.assertEqual(cfg["property"]["deductible_mortgage_interest"],
                         340_000 * _mortgage(doc)["rate"])

    def test_absence_of_the_flag_adds_no_key(self):
        """DP#32: a contract that never declares `deductible` (the golden
        path and every pre-#1075 document) maps byte-identically -- no
        deductible_mortgage_balance / deductible_mortgage_interest key, no
        fabricated zero."""
        doc = _load_doc()
        cfg = ic.to_internal_config(doc)
        self.assertNotIn("deductible_mortgage_balance", cfg["property"])
        self.assertNotIn("deductible_mortgage_interest", cfg["property"])

    def test_deductible_balance_and_interest_reach_the_config_and_round_trip(self):
        """The keys are not adapter-only: SimulationConfig carries them
        (from_dict), to_dict re-emits them (DP#24), and the interest is the
        declared tranche's OWN-rate math the #850 pricing will deduct."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "invest_a", 30_000, 0.065, deductible=True)]
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(sim_cfg.deductible_mortgage_balance, 30_000)
        self.assertEqual(
            sim_cfg.to_dict()["property"]["deductible_mortgage_balance"], 30_000)
        # The #850 pricing input: the DECLARED tranche's annual interest is
        # its OWN-rate math -- 30,000 @ 6.5% = 1,950 -- NOT
        # deductible_balance * the weighted mortgage_rate, which only
        # coincides with the true interest when every tranche shares one
        # rate (the house tranche's 4.5% makes it 30,000 * 0.0466... =
        # ~1,399 here, a fabricated figure the s.20(1)(c) deduction must
        # never price).
        self.assertEqual(sim_cfg.deductible_mortgage_interest, 30_000 * 0.065)
        self.assertAlmostEqual(sim_cfg.deductible_mortgage_interest, 1950.0)
        self.assertNotAlmostEqual(
            sim_cfg.deductible_mortgage_interest,
            sim_cfg.deductible_mortgage_balance * sim_cfg.mortgage_rate)
        # The interest field round-trips too (DP#24), same absence-safe
        # convention as the balance.
        self.assertEqual(
            sim_cfg.to_dict()["property"]["deductible_mortgage_interest"], 1950.0)
        # ... while the golden-style absent case round-trips WITHOUT the keys.
        plain = SimulationConfig.from_dict({
            "family": {"members": [], "children": []},
            "property": {"house_value": 650_000, "mortgage_balance": 340_000,
                         "mortgage_rate": 0.045},
            "assumptions": {"start_year": 2026},
            "savings": {"rate": 0.0}, "tax": {"province": "qc"},
        })
        self.assertEqual(plain.deductible_mortgage_balance, 0.0)
        self.assertEqual(plain.deductible_mortgage_interest, 0.0)
        self.assertNotIn("deductible_mortgage_balance", plain.to_dict()["property"])
        self.assertNotIn("deductible_mortgage_interest", plain.to_dict()["property"])


# ============================================================================
# (c) cash_back: credited as an origination cash-flow in the first year
# ============================================================================

class TestCashBack(unittest.TestCase):
    def test_cash_back_is_credited_at_origination(self):
        """A $1,200 cash-back on the house tranche becomes an
        ``origination_cash_back`` cash-flow in the projection's FIRST year
        (start_year -- the same calendar-year spelling every declared
        cash_flow uses, so the engine's cf['year'] == sim_year test fires it
        at year 0), positive to the household, non-taxable (a lender rebate,
        not income). The tranche sits in a multi-tranche charge (a second
        investment tranche beside it)."""
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                       "term_years": 5}
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "invest_tranche", 10_000, 0.065)]
        cfg = ic.to_internal_config(doc)
        start_year = int(doc["as_of"][:4])
        inflow = next(cf for cf in cfg["cash_flows"]
                      if cf["year"] == start_year and cf["amount"] == 1200.0)
        self.assertEqual(inflow["tax_treatment"], "non-taxable")
        # EXACTLY ONE entry carries the cash-back amount at start_year -- the
        # origination inflow is its own list entry, so the count stays 1 even
        # if the example later gains an unrelated start-year cash-flow.
        self.assertEqual(
            sum(1 for cf in cfg["cash_flows"]
                if cf["year"] == start_year and cf["amount"] == 1200.0), 1)

    def test_cash_back_on_a_single_mortgage_is_credited_too(self):
        """The field is not multi-tranche-only: one mortgage with a cash_back
        block gets the same origination inflow."""
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                       "term_years": 5}
        cfg = ic.to_internal_config(doc)
        start_year = int(doc["as_of"][:4])
        inflow = next(cf for cf in cfg["cash_flows"]
                      if cf["year"] == start_year and cf["amount"] == 1200.0)
        self.assertEqual(inflow["tax_treatment"], "non-taxable")

    def test_multiple_cash_backs_sum_into_one_inflow(self):
        """Each tranche's declared amount joins the single origination
        inflow (the inflow is linear, so summing is exact)."""
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                       "term_years": 5}
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "invest_tranche", 10_000, 0.065,
                     cash_back={"amount": 250.0, "clawback_rate": 0.0,
                                "term_years": 3}),
        ]
        cfg = ic.to_internal_config(doc)
        start_year = int(doc["as_of"][:4])
        total = sum(cf["amount"] for cf in cfg["cash_flows"]
                    if cf["year"] == start_year)
        self.assertEqual(total, 1200.0 + 250.0)

    def test_no_cash_back_adds_no_cash_flow(self):
        """DP#32: absence of the block is 'no incentive' -- the cash_flows
        list is byte-identical to the example's own declared flows."""
        doc = _load_doc()
        cfg = ic.to_internal_config(doc)
        self.assertEqual(
            cfg["cash_flows"],
            [{"year": int(cf["date"][:4]), "amount": cf["amount"],
              "tax_treatment": "non-taxable" if cf["tax_treatment"] == "tax_free"
              else "post-tax"}
             for cf in doc.get("cash_flows", [])])

    def test_a_conditional_cash_back_carries_its_house_condition_on_the_flow(self):
        """Issue #1075 (optimizer half): a cash_back declaring
        ``min_house_amount`` is CONDITIONAL on the swept house tranche -- the
        threshold rides ON the origination cash-flow (the sweep's cell
        composition withholds the inflow below it), and a cash_back that
        declares none carries NO such key (DP#13/DP#32: absence of the key
        IS the marker -- the credit stays unconditional, byte-identical to
        pre-#1075)."""
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                       "term_years": 5,
                                       "min_house_amount": 600_000}
        cfg = ic.to_internal_config(doc)
        start_year = int(doc["as_of"][:4])
        flow = next(cf for cf in cfg["cash_flows"]
                    if cf["year"] == start_year and cf["amount"] == 1200.0)
        self.assertEqual(flow["min_house_amount"], 600_000)

        # Absent condition -> no key on the flow (unconditional credit).
        doc2 = _load_doc()
        _mortgage(doc2)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                        "term_years": 5}
        cfg2 = ic.to_internal_config(doc2)
        flow2 = next(cf for cf in cfg2["cash_flows"]
                     if cf["year"] == start_year and cf["amount"] == 1200.0)
        self.assertNotIn("min_house_amount", flow2)

    def test_the_summed_credit_uses_the_strictest_house_condition(self):
        """The aggregated origination inflow is ONE credit, so its condition
        is the STRICTEST declared one: a $600k-threshold house tranche plus a
        $650k-threshold investment tranche yields one $1,450 inflow that is
        credited only for a house at/above $650k -- the whole credit is
        withheld below it, never a partial credit (DP#32: the sweep reports
        one verdict, so the condition must be one threshold)."""
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                       "term_years": 5,
                                       "min_house_amount": 600_000}
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "invest_tranche", 10_000, 0.065,
                     cash_back={"amount": 250.0, "clawback_rate": 0.0,
                                "term_years": 3,
                                "min_house_amount": 650_000}),
        ]
        cfg = ic.to_internal_config(doc)
        start_year = int(doc["as_of"][:4])
        flow = next(cf for cf in cfg["cash_flows"]
                    if cf["year"] == start_year and cf["amount"] == 1450.0)
        self.assertEqual(flow["min_house_amount"], 650_000)

    def test_the_inflow_reaches_the_engine_at_year_zero(self):
        """The credited cash-back is not config-only prose: running the
        engine, year-0 annual_savings rises by exactly the inflow (the
        household receives the cash at origination). Years after year 0 are
        unchanged by the inflow itself."""
        def _run(with_cash_back):
            doc = _load_doc()
            _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 0.5,
                                           "term_years": 5}
            if not with_cash_back:
                del _mortgage(doc)["cash_back"]
            legacy = ic.to_internal_config(doc)
            sim_cfg = SimulationConfig.from_dict(legacy)
            sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg))
            return sim.run()

        with_inflow = _run(with_cash_back=True)
        without_inflow = _run(with_cash_back=False)
        self.assertAlmostEqual(
            with_inflow[0].annual_savings - without_inflow[0].annual_savings,
            1200.0)
        # Every later year is identical -- the inflow is a one-time year-0
        # credit, not an annual subsidy.
        for w, o in zip(with_inflow[1:], without_inflow[1:]):
            self.assertEqual(w.annual_savings, o.annual_savings)


# ============================================================================
# (d) DP#32: the refusals that must still fire
# ============================================================================

class TestLoudRefusals(unittest.TestCase):
    def test_a_charge_exceeded_by_the_summed_tranches_is_refused(self):
        """Two tranches summing past the 80% charge (520,000 for the
        example's 650,000 house) are refused loudly, never clamped: the
        summed 540,000 + 150,000 HELOC = 690,000 > 520,000. The refusal is
        the #664/#689 charge check, fed the SUM -- so the multi-tranche
        model cannot be used to launder a >80% facility past the guard."""
        doc = _load_doc()
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(_mortgage(doc), "too_big", 200_000, 0.05)]
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("charge", msg)
        self.assertIn("520,000", msg)

    def test_different_collaterals_are_refused(self):
        """Two mortgages on DIFFERENT charges are two debts, not two
        tranches of one -- refused loudly rather than summed into a
        fabricated single mortgage (DP#32)."""
        doc = _load_doc()
        template = _mortgage(doc)
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "mortgage"]
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(template, "m_rental_a", 100_000, 0.05,
                     collateral="rental_duplex"),
            _tranche(template, "m_rental_b", 50_000, 0.05,
                     collateral="cottage"),
        ]
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("DIFFERENT collaterals", str(ctx.exception))

    def test_two_tranches_sharing_a_non_principal_charge_are_refused(self):
        """The #1075 lift is PRINCIPAL-ONLY: the summed facility lands in the
        principal's mortgage_balance, so two tranches of a charge the
        principal does NOT secure (here: the rental duplex) must never be
        folded into the family home's debt. The unfiltered fallback keeps
        the #652 loud >1 refusal (DP#32) -- this is the document shape that
        used to load silently by summing a rental charge into the
        principal's balance."""
        doc = _load_doc()
        template = _mortgage(doc)
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "mortgage"]
        doc["liabilities"] = doc["liabilities"] + [
            _tranche(template, "rental_tranche_a", 100_000, 0.05,
                     collateral="rental_duplex"),
            _tranche(template, "rental_tranche_b", 50_000, 0.05,
                     collateral="rental_duplex"),
        ]
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("rental_tranche_a", msg)
        self.assertIn("rental_tranche_b", msg)
        self.assertIn("principal", msg)

    def test_zero_total_balance_is_refused(self):
        """The weighted-average rate is undefined for a zero total (0/0) --
        refused loudly rather than guessed (DP#32)."""
        doc = _load_doc()
        mortgage = _mortgage(doc)
        mortgage["balance"]["amount"] = 0
        doc["liabilities"] = [mortgage] + [
            _tranche(mortgage, "also_zero", 0, 0.05)]
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("balance-weighted average rate is undefined",
                      str(ctx.exception))


# ============================================================================
# (e) A single-mortgage contract loads byte-identically
# ============================================================================

class TestSingleMortgageUnchanged(unittest.TestCase):
    def test_the_example_mortgage_maps_exactly_as_before(self):
        """The example document (one mortgage, no new flags) produces the
        same property facts and the same cash_flows as pre-#1075 -- DP#13:
        absence of the new fields is not an opinion."""
        doc = _load_doc()
        cfg = ic.to_internal_config(doc)
        mortgage = _mortgage(doc)
        self.assertEqual(cfg["property"]["mortgage_balance"],
                         mortgage["balance"]["amount"])
        self.assertEqual(cfg["property"]["mortgage_rate"], mortgage["rate"])
        self.assertEqual(cfg["property"]["amortization_years"],
                         mortgage["amortization"]["years"])
        self.assertNotIn("deductible_mortgage_balance", cfg["property"])
        self.assertNotIn("deductible_mortgage_interest", cfg["property"])
        # The two declared example cash-flows, unchanged, with no appended
        # origination inflow.
        self.assertEqual(len(cfg["cash_flows"]), len(doc.get("cash_flows", [])))

    def test_an_explicit_false_flag_is_still_a_no_op(self):
        """`deductible: false` / a cash_back with amount 0 are real values
        meaning 'not deductible' / 'no inflow' -- the config carries neither
        a deductible balance nor an origination inflow (DP#32: 0 is a
        value, not a fallback)."""
        doc = _load_doc()
        _mortgage(doc)["deductible"] = False
        _mortgage(doc)["cash_back"] = {"amount": 0.0, "clawback_rate": 0.0,
                                       "term_years": 5}
        cfg = ic.to_internal_config(doc)
        self.assertNotIn("deductible_mortgage_balance", cfg["property"])
        self.assertNotIn("deductible_mortgage_interest", cfg["property"])
        self.assertEqual(len(cfg["cash_flows"]), len(doc.get("cash_flows", [])))


# ============================================================================
# Schema: the new fields are mortgage-only and validated
# ============================================================================

class TestSchemaShape(unittest.TestCase):
    def test_cash_back_is_rejected_on_a_heloc(self):
        doc = _load_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["cash_back"] = {"amount": 100.0, "clawback_rate": 0.5,
                              "term_years": 3}
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_deductible_is_rejected_on_a_heloc(self):
        doc = _load_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["deductible"] = True
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_cash_back_requires_all_three_fields(self):
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0}  # no clawback_rate/term_years
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_cash_back_clawback_rate_must_be_a_fraction(self):
        doc = _load_doc()
        _mortgage(doc)["cash_back"] = {"amount": 1200.0, "clawback_rate": 1.5,
                                       "term_years": 5}
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_deductible_must_be_a_boolean(self):
        doc = _load_doc()
        _mortgage(doc)["deductible"] = "yes"
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)


if __name__ == "__main__":
    unittest.main()
