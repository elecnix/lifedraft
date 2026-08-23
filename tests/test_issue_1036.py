#!/usr/bin/env python3
"""Issue #1036: standalone borrow-to-invest on a mortgage-free property.

The engine modelled leverage ONLY as the readvanceable Smith Manoeuvre (a
mortgage principal paydown readvanced into a HELOC). A mortgage-free
household that wanted to borrow against home equity, invest the proceeds in
a non-registered account, and deduct the interest under ITA s.20(1)(c) had
no way to express it: with no mortgage there is nothing to readvance, so
the run silently produced an UNLEVERAGED trajectory (the worst part of the
bug -- a confident wrong number, DP#32).

This file locks the three acceptance criteria:

1. A mortgage-free household can declare a HELOC with a limit, have the
   optimizer choose a draw amount from a ladder (``decisions.borrow_to_invest``),
   invest it non-registered, and see the interest deduction and the leveraged
   pot in the trajectory -- with no readvanceable facility anywhere.
2. Leverage is either modelled or refused with a reason; the engine no longer
   silently returns an unleveraged answer.
3. ``heloc.balance``, ``heloc.deductibility`` and ``capitalize_interest`` are
   each either read or loudly refused; the schema-coverage DEAD_ALLOWLIST
   entries are removed (covered by tests/test_schema_coverage.py).

DP#15: no personal data. Fixtures use the shipped example contract
(``schema/example.json``), trimmed to the two-generation couple, with
fabricated round-number modifications -- nothing from any real contract.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy
import json
import tempfile
import unittest

import input_contract as ic
import optimize
import output_paths
from objective import MAX_NET_BENEFIT
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter


def _example_doc():
    """The shipped example contract, trimmed to the two-generation couple
    (the same helper tests/test_input_contract.py and tests/test_issue_663
    _no_heloc.py use). All figures are the shipped example's fabricated round
    numbers (DP#15)."""
    from test_input_contract import _load_example, _two_generation_subset
    return _two_generation_subset(_load_example())


def _mortgage_free_doc():
    """The example with the mortgage removed -- a mortgage-free household
    that keeps its HELOC (readvanceable=false: no mortgage to readvance
    against) and its unsecured line_of_credit. The motivating case for #1036."""
    doc = _example_doc()
    doc["liabilities"] = [
        dict(l, readvanceable=False) if l["kind"] == "heloc" else l
        for l in doc["liabilities"]
        if l["kind"] in ("heloc", "line_of_credit")
    ]
    return doc


def _btv_option(id_, label, amount, source="heloc_main", target="non_reg"):
    return {"id": id_, "label": label, "source": source,
            "amount": amount, "target_account": target}


class TestBorrowToInvestIsModelled(unittest.TestCase):
    """Acceptance #1: a mortgage-free household declares a HELOC + a
    borrow-to-invest amount ladder, and the optimizer produces a LEVERAGED,
    RANKED trajectory -- no readvanceable facility anywhere."""

    def setUp(self):
        import output_paths as _op
        self._orig_cache = _op.CACHE_DIR
        _op.CACHE_DIR = tempfile.mkdtemp()

    def tearDown(self):
        import output_paths as _op
        _op.CACHE_DIR = self._orig_cache

    def _run(self, doc):
        cfg = ic.to_internal_config(doc)
        return optimize.run_borrow_to_invest_exploration(
            cfg, "input.json", objective=MAX_NET_BENEFIT)

    def test_mortgage_free_household_with_borrow_to_invest_is_leveraged(self):
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw $50k", 50_000),
            _btv_option("btv_100k", "Draw $100k", 100_000),
        ]
        results = self._run(doc)
        self.assertTrue(results, "expected ranked borrow-to-invest results")

        # The no-draw baseline is always present (DP#33: the frame of reference).
        ids = {r["borrow_to_invest_id"] for r in results}
        self.assertEqual(ids, {"no_draw", "btv_50k", "btv_100k"})

        # The $100k draw is LEVERAGED: year-1 heloc_balance > 0 and the
        # s.20(1)(c) interest deduction fires (margin_deductible_interest > 0).
        winner = max(
            (r for r in results if r["borrow_to_invest_id"] == "btv_100k"),
            key=lambda r: r.get("objective_score", r.get("net_benefit", 0)))
        yy = winner.get("year_by_year") or []
        self.assertGreater(len(yy), 1)
        y1 = yy[1]
        self.assertGreater(y1["heloc_balance"], 0.0,
                          "a $100k draw must book a HELOC balance > 0")
        self.assertGreater(y1["margin_deductible_interest"], 0.0,
                          "the borrowed-to-invest interest must be deducted "
                          "under s.20(1)(c) (margin_deductible_interest > 0)")

        # The no-draw baseline is UNLEVERAGED: heloc_balance stays 0.
        baseline = next(r for r in results if r["borrow_to_invest_id"] == "no_draw")
        yy0 = baseline.get("year_by_year") or []
        self.assertEqual(yy0[1]["heloc_balance"], 0.0)

    def test_draw_amount_ranks_against_the_no_draw_baseline(self):
        """DP#33: the declared ladder does not replace the sweep -- the
        no-draw baseline is the frame of reference every draw is read against.
        For this fabricated positive-spread household, drawing beats not
        drawing, and more draw beats less."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw $50k", 50_000),
            _btv_option("btv_100k", "Draw $100k", 100_000),
        ]
        results = self._run(doc)
        winners = optimize.winners_by_borrow_to_invest(results)
        # Winners are ranked by the active objective (best first), so the
        # no-draw baseline is NOT necessarily row 1 -- it is the frame of
        # reference every draw is read against (DP#33), ranked on its merits.
        self.assertEqual([w["id"] for w in winners],
                         ["btv_100k", "btv_50k", "no_draw"])
        by_id = {w["id"]: w for w in winners}
        self.assertGreater(by_id["btv_100k"]["net_benefit"],
                           by_id["no_draw"]["net_benefit"],
                           "the $100k draw must beat the no-draw baseline "
                           "for this positive-spread household")
        self.assertGreater(by_id["btv_100k"]["net_benefit"],
                           by_id["btv_50k"]["net_benefit"])

    def test_absent_borrow_to_invest_is_a_strict_noop(self):
        """DP#32: a household that declares no borrow-to-invest gets NO sweep
        -- run_borrow_to_invest_exploration returns [], and the golden
        trajectory is byte-identical (the gate in main() never calls it)."""
        doc = _mortgage_free_doc()  # no decisions.borrow_to_invest
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg.get("borrow_to_invest_options"), None)
        self.assertEqual(
            optimize.run_borrow_to_invest_exploration(cfg, "input.json"),
            [])

    def test_borrow_to_invest_with_no_refinance_advance_split(self):
        """N1 coverage: when the contract declares NO #792 refinance advance
        split (property.refinance_advance_deductible_non_reg absent), the
        borrow-to-invest draw's non_reg front-load starts from 0 (the
        'existing_split is None' branch) -- the draw still routes the whole
        amount to non_reg. Covers the optimize.py guard the coverage gate
        flagged (uncovered +1)."""
        doc = _mortgage_free_doc()
        # Strip every refinance option's advance_split so no #792 split is
        # declared -> property.refinance_advance_deductible_non_reg is absent.
        for opt in doc["decisions"]["mortgage"].get("refinance_options", []):
            opt.pop("advance_split", None)
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k into non-reg", 50_000)]
        cfg = ic.to_internal_config(doc)
        self.assertNotIn("refinance_advance_deductible_non_reg", cfg["property"])
        results = optimize.run_borrow_to_invest_exploration(
            cfg, "input.json", objective=MAX_NET_BENEFIT)
        # The draw ran (the no-advance-split branch did not short-circuit).
        self.assertTrue(any(r["borrow_to_invest_id"] == "btv_50k" for r in results))

    def test_print_borrow_to_invest_report_empty_is_a_noop(self):
        """The report's empty-results guard (mirrors
        _print_property_funding_report): called with no results, it returns
        without printing a header-for-nothing. Covers the guard so the
        coverage gate's per-file ratchet does not flag it."""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_borrow_to_invest_report([])
        self.assertEqual(buf.getvalue(), "")

    def test_no_silent_smith_manoeuvre_unavailable_for_a_borrow_to_invest_household(self):
        """Acceptance #2: a mortgage-free household declaring borrow-to-invest
        must NOT see 'Smith Manoeuvre unavailable' silence -- leverage is
        modelled, not refused. (The 'unavailable' notice is correct for a
        no-HELOC household, but this household HAS a HELOC and IS leveraged.)"""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [_btv_option("btv_100k", "Draw $100k", 100_000)]
        results = self._run(doc)
        leveraged = [r for r in results if r["borrow_to_invest_id"] == "btv_100k"]
        self.assertTrue(leveraged)
        self.assertGreater(
            max(r.get("net_benefit", 0) for r in leveraged), 0)

    def test_main_prints_borrow_to_invest_ranking_for_a_mortgage_free_household(self):
        """The main() gate prints the borrow-to-invest ranking when the
        household declares it -- a mortgage-free household runs end-to-end
        through main() without crashing (the apply_overlay mortgage-balance
        fix) and the ranking table is surfaced (issue #1036 acceptance #1/#2)."""
        import io
        import contextlib
        import tempfile as _tmp
        d = _tmp.mkdtemp()  # for the input.json file (CACHE_DIR is restored by setUp/tearDown)
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k into non-reg", 50_000),
            _btv_option("btv_100k", "Draw 100k into non-reg", 100_000),
        ]
        path = os.path.join(d, "input.json")
        with open(path, "w") as f:
            json.dump(doc, f)
        old_argv = sys.argv
        sys.argv = ["optimize.py", "--input", path]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                optimize.main()
        finally:
            sys.argv = old_argv
        out = buf.getvalue()
        self.assertIn("BORROW-TO-INVEST RANKING", out)
        self.assertIn("Draw 100k into non-reg", out)
        self.assertIn("No draw (baseline)", out)


class TestBorrowToInvestValidation(unittest.TestCase):
    """DP#32: a partial/unsupported borrow-to-invest declaration must FAIL
    LOUDLY at load, never silently coerce to the supported value."""

    def _load(self, doc):
        ic.validate_contract(doc)
        return ic.to_internal_config(doc)

    def test_source_must_resolve_to_a_heloc(self):
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_x", "Draw", 50_000, source="personal_loc")]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("personal_loc", str(cm.exception))

    def test_source_must_be_the_facility_heloc(self):
        """The engine models ONE HELOC facility: a document declaring two
        kind=heloc liabilities is refused loudly (the existing single-facility
        charge check) before any borrow-to-invest source can name the second.
        That refusal is the loud gate; a draw against a non-facility heloc can
        never silently no-op (DP#32)."""
        doc = _mortgage_free_doc()
        second = copy.deepcopy(
            next(l for l in doc["liabilities"] if l["kind"] == "heloc"))
        second["id"] = "heloc_second"
        second["limit"] = 40_000
        doc["liabilities"].append(second)
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_x", "Draw", 10_000, source="heloc_second")]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("exactly one heloc facility", str(cm.exception))

    def test_source_cannot_alias_a_non_principal_heloc(self):
        """D5: a second HELOC on a NON-principal (recreational) property passes
        the single-facility charge check (which scopes to the principal), so
        `heloc_limits` can hold two entries. A borrow_to_invest source naming
        the recreational heloc must be REFUSED -- the draw books on the
        principal facility (new_heloc_balance), at the principal rate/charge,
        so accepting it would silently alias another liability (DP#32)."""
        doc = _mortgage_free_doc()
        # A recreational property the couple owns, + a HELOC on it.
        doc["properties"].append({
            "id": "cottage", "kind": "recreational",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "value": {"amount": 200_000, "as_of": "2026-06-30"},
            "acb": 100_000, "designated_principal_residence_years": [],
        })
        second = copy.deepcopy(
            next(l for l in doc["liabilities"] if l["kind"] == "heloc"))
        second["id"] = "heloc_cottage"
        second["limit"] = 60_000
        second["collateral"] = "cottage"
        doc["liabilities"].append(second)
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_x", "Draw", 10_000, source="heloc_cottage")]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("single drawn HELOC facility", str(cm.exception))

    def test_borrow_to_invest_with_no_heloc_is_refused(self):
        """A mortgage-free, HELOC-free household declaring borrow-to-invest is
        refused loudly -- there is nothing to draw against, never a silent
        unleveraged answer (DP#32 / issue #1036)."""
        doc = _mortgage_free_doc()
        doc["liabilities"] = [l for l in doc["liabilities"]
                              if l["kind"] != "heloc"]
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_x", "Draw", 50_000, source="heloc_main")]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("no kind=heloc liability with that id", str(cm.exception))

    def test_amount_over_limit_is_refused(self):
        doc = _mortgage_free_doc()
        # heloc_main limit is 150000 in the shipped example.
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_x", "Draw", 200_000, source="heloc_main")]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("over-limit", str(cm.exception).lower())

    def test_amount_zero_or_negative_is_refused(self):
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_zero", "Draw zero", 0, source="heloc_main")]
        with self.assertRaises(ic.ContractAdaptationError):
            self._load(doc)

    def test_target_account_other_than_non_reg_is_refused(self):
        """A registered target (RRSP/TFSA) is non-deductible under s.18(11);
        the schema's enum ("non_reg" only) refuses it loudly at validation,
        never silently coerced (DP#32)."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_rrsp", "Draw into RRSP", 50_000, target="rrsp")]
        with self.assertRaises(ic.ContractValidationError) as cm:
            self._load(doc)
        self.assertIn("target_account", str(cm.exception))

    def _btv_cfg(self, doc):
        """Load a borrow-to-invest doc to the internal config (no refusal at
        load -- the D2 refusal is at the EXPLORATION invocation point, where
        the resolved objective is known)."""
        return ic.to_internal_config(doc)

    def test_borrow_to_invest_with_liquidate_to_target_is_refused(self):
        """D2 / #1037: borrow-to-invest + die-with-zero liquidates the borrowed
        pot while the HELOC is never repaid. Refuse at the exploration point
        (DP#32)."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k", 50_000)]
        doc["assumptions"]["retirement"]["liquidate_to_target"] = True
        cfg = self._btv_cfg(doc)  # loads fine -- the refusal is at exploration
        with self.assertRaises(ValueError) as cm:
            optimize.run_borrow_to_invest_exploration(cfg, "input.json")
        msg = str(cm.exception)
        self.assertIn("liquidate_to_target", msg)
        self.assertIn("#1037", msg)

    def test_borrow_to_invest_with_min_after_tax_estate_is_refused(self):
        """D2 / #1081: the root cause is NOT liquidate_to_target -- it is that
        min_after_tax_estate scores -net_estate, and outstanding borrow-to-invest
        HELOC debt reduces net_estate dollar-for-dollar, so 'borrow the maximum
        and die owing it' ranks highest (the inverted incentive filed as
        #1081, reproduced with liquidate_to_target ABSENT). #1068 (the #1065
        insolvency floor) does NOT cover it (every net_estate is positive, so
        its branch never fires). Refuse min_after_tax_estate regardless of
        liquidate_to_target, at the exploration point (DP#32)."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_150k", "Draw 150k", 150_000)]
        doc["decisions"]["objective"] = "min_after_tax_estate"  # liquidate_to_target ABSENT
        cfg = self._btv_cfg(doc)
        from objective import get_objective
        with self.assertRaises(ValueError) as cm:
            optimize.run_borrow_to_invest_exploration(
                cfg, "input.json", objective=get_objective("min_after_tax_estate"))
        msg = str(cm.exception)
        self.assertIn("min_after_tax_estate", msg)
        self.assertIn("#1081", msg)

    def test_borrow_to_invest_min_after_tax_estate_cli_override_is_refused(self):
        """D2: the refusal is at the EXPLORATION point so it catches a
        `--objective min_after_tax_estate` CLI override -- which a load-time
        check CANNOT see (the objective is resolved per-run, not per-document).
        The contract declares the default objective; the CLI overrides to
        min_after_tax_estate; the exploration refuses."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k", 50_000)]
        cfg = self._btv_cfg(doc)  # default objective, loads fine
        from objective import get_objective
        with self.assertRaises(ValueError) as cm:
            optimize.run_borrow_to_invest_exploration(
                cfg, "input.json", objective=get_objective("min_after_tax_estate"))
        self.assertIn("min_after_tax_estate", str(cm.exception))

    def test_borrow_to_invest_under_default_objective_runs(self):
        """The refusal targets the debt-distorted regimes (min_after_tax_estate,
        liquidate_to_target); borrow-to-invest runs under the default
        max_net_benefit / wealth-maximising objectives."""
        doc = _mortgage_free_doc()
        doc["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k", 50_000)]
        cfg = self._btv_cfg(doc)  # default objective = max_net_benefit
        results = optimize.run_borrow_to_invest_exploration(
            cfg, "input.json", objective=MAX_NET_BENEFIT)
        self.assertTrue(any(r["borrow_to_invest_id"] == "btv_50k" for r in results))
        doc2 = _mortgage_free_doc()
        doc2["decisions"]["borrow_to_invest"] = [
            _btv_option("btv_50k", "Draw 50k", 50_000)]
        doc2["decisions"]["objective"] = "max_after_tax_estate"
        from objective import get_objective
        cfg2 = self._btv_cfg(doc2)
        results2 = optimize.run_borrow_to_invest_exploration(
            cfg2, "input.json", objective=get_objective("max_after_tax_estate"))
        self.assertTrue(any(r["borrow_to_invest_id"] == "btv_50k" for r in results2))


class TestHelocDeclarationsRefusedOrRead(unittest.TestCase):
    """Acceptance #3: heloc.balance, heloc.deductibility, and
    capitalize_interest are each either read or loudly refused (DP#32)."""

    def _load(self, doc):
        ic.validate_contract(doc)
        return ic.to_internal_config(doc)

    def test_heloc_opening_drawn_balance_without_deductibility_refused(self):
        """Issue #1039 replaced #1036's blanket refusal with HONOURING an
        opening drawn position -- but only when its s.20(1)(c) trace is
        derivable. A declared balance WITHOUT a deductibility block still
        refuses loudly (DP#32: defaulting the trace would fabricate a tax
        position); see tests/test_issue_1039.py for the honoured path."""
        doc = _example_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["balance"]["amount"] = 65_000
        del heloc["deductibility"]
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("OPENING DRAWN balance", str(cm.exception))

    def test_heloc_zero_balance_is_accepted(self):
        """balance = 0 (undrawn) is the documented accepted state (#577)."""
        doc = _example_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["balance"]["amount"] = 0
        cfg = self._load(doc)  # must not raise
        self.assertEqual(cfg["property"]["margin_available"], 150_000)

    def test_heloc_deductibility_refused(self):
        """The s.20(1)(c) trace is COMPUTED from the borrowing's purpose, never
        a declared ratio on the HELOC -- a declared investment_portion > 0 is
        refused loudly (same stance as the consumer-loan path, whose message
        instructs 'declare investment_portion=0')."""
        doc = _example_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["deductibility"] = {"investment_portion": 0.6, "personal_portion": 0.4}
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._load(doc)
        self.assertIn("deductibility", str(cm.exception))

    def test_heloc_deductibility_zero_investment_portion_is_accepted(self):
        """D1: a pure personal-use declaration (investment_portion=0) is the
        safe, accepted state -- the user's real contracts declare exactly
        {investment_portion: 0.0, personal_portion: 1.0}. Refusing it would
        block every production contract. Accepted (the trace is computed from
        the borrowing's purpose, so a 0 investment_portion asserts nothing
        deductible and reaches no decision)."""
        doc = _example_doc()
        heloc = next(l for l in doc["liabilities"] if l["kind"] == "heloc")
        heloc["deductibility"] = {"investment_portion": 0.0, "personal_portion": 1.0}
        cfg = self._load(doc)  # must not raise
        self.assertEqual(cfg["property"]["margin_available"], 150_000)

    def test_capitalize_interest_is_read_into_the_config(self):
        doc = _example_doc()  # helocMain declares capitalize_interest: false
        cfg = self._load(doc)
        sc = SimulationConfig.from_dict(cfg)
        self.assertFalse(sc.capitalize_interest,
                         "the example heloc declares capitalize_interest=false; "
                         "it must be read into SimulationConfig.capitalize_interest")
        # And it round-trips (DP#24).
        self.assertFalse(SimulationConfig.from_dict(sc.to_dict()).capitalize_interest)


class TestCapitalizeInterestEngineBehaviour(unittest.TestCase):
    """Issue #1036: capitalize_interest now SELECTS between capitalizing and
    cash-servicing the drawn-margin interest (it used to be silently dropped).

    Built on internal configs (the same shape tests/test_issue_577 uses) so the
    golden fixture -- which never sets the key -- stays byte-identical (default
    True = the pre-#1036 capitalization path).
    """

    def _config(self, capitalize_interest, margin_available=200_000,
                lump_sum=100_000):
        # No RRSP room and no savings -> no RRSP-refund HELOC paydown, so the
        # draw holds and the capitalize-vs-service split is observable directly.
        return SimulationConfig(
            projection_years=3,
            investment_return=0.06,
            salary_growth=0.0,
            savings_rate=0.0,
            house_value=500_000,
            mortgage_balance=100_000,
            mortgage_rate=0.05,
            ltv_max=0.80,
            amortization_years=20,
            margin_available=margin_available,
            heloc_readvance=False,
            capitalize_interest=capitalize_interest,
            heloc_rate=0.05,
            family_members=[
                {"role": "primary", "gross_income": 120_000,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1985},
                {"role": "spouse", "gross_income": 60_000,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1987},
            ],
            children=[],
        )

    def _run(self, config, lump_sum):
        sim = FamilySimulation(config, adapter=CanadaAdapter(config),
                               use_readvanceable=False, deduct_later=False,
                               lump_sum=lump_sum)
        return sim.run()

    def test_capitalize_true_capitalizes_interest_into_the_balance(self):
        config = self._config(capitalize_interest=True)
        results = self._run(config, lump_sum=100_000)
        y1 = results[0]
        self.assertGreater(y1.heloc_interest_capitalized, 0.0,
                            "capitalize_interest=True must capitalize the "
                            "drawn-margin interest (up to the charge room)")
        # The balance grew by the capitalized interest (no paydown here).
        self.assertGreater(y1.heloc_balance, 100_000.0)

    def test_capitalize_false_services_interest_in_cash(self):
        config = self._config(capitalize_interest=False)
        results = self._run(config, lump_sum=100_000)
        y1 = results[0]
        self.assertEqual(y1.heloc_interest_capitalized, 0.0,
                         "capitalize_interest=False must NOT capitalize any "
                         "interest -- it is serviced in cash")
        self.assertGreater(y1.heloc_interest_serviced, 0.0,
                           "the interest must be serviced in cash instead")
        # The balance did NOT grow by interest (it is held flat; only the
        # paydown/servicing rules move it, and there is no paydown here).
        self.assertLessEqual(y1.heloc_balance, 100_000.0 + 1e-6)

    def test_capitalize_interest_defaults_to_true(self):
        """An internal config that never sets the key (the golden fixture's
        shape) defaults to True -- byte-identical to the pre-#1036
        capitalization path (DP#32: absence is the fallback)."""
        config = self._config(capitalize_interest=True)
        config2 = copy.copy(config)
        # Simulate "key absent" by constructing from a dict that omits it.
        d = config.to_dict()
        # capitalize_interest is only re-emitted when not True, so to_dict()
        # already omits it here -- round-trip through from_dict must default
        # back to True.
        reloaded = SimulationConfig.from_dict(d)
        self.assertTrue(reloaded.capitalize_interest)


class TestGoldenInvariantUnchanged(unittest.TestCase):
    """The golden household fixture does not declare decisions.borrow_to_invest
    and builds its internal `property` dict directly (it bypasses
    input_contract.py), so it cannot hit any new code path: no borrow-to-invest
    sweep runs, no HELOC declaration is refused, and capitalize_interest is
    absent -> default True (the pre-#1036 capitalization path). The trajectory
    stays byte-exact."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        """The golden fixture does not declare decisions.borrow_to_invest and
        builds its internal `property` dict directly (bypasses input_contract),
        so this PR cannot move it. The byte-exact value is whatever current
        origin/main produces (main advanced via #1075/#1046/#1057 while this
        branch was open, moving the golden to 9709753.139463063); this branch,
        rebased onto that main, reproduces it identically -- the PR adds no
        movement. (The 9816435.13530067 value in earlier revisions was the
        golden at the branch's start; it is stale.)"""
        from test_golden_trajectory_581 import golden_household_config, _run
        self.assertEqual(
            repr(_run(golden_household_config())[-1].total_assets),
            "9709753.139463063")

    def test_golden_config_defaults_capitalize_interest_true(self):
        from test_golden_trajectory_581 import golden_household_config
        from simulation_config import SimulationConfig
        sc = SimulationConfig.from_dict(golden_household_config())
        self.assertTrue(sc.capitalize_interest,
                        "the golden fixture's internal dict carries no "
                        "capitalize_interest key, so it must default to True "
                        "(the pre-#1036 capitalization path, byte-identical)")
        # D7: the raw cfg key (not a SimulationConfig field) is the one
        # spelling the optimizer reads; the golden fixture does not set it.
        self.assertIsNone(golden_household_config().get("borrow_to_invest_options"))


if __name__ == "__main__":
    unittest.main()

class TestUnfundedInterestNotDeducted(unittest.TestCase):
    """Issue #1036 D4/N2: the drawn-margin s.20(1)(c) deduction must EXCLUDE
    `heloc_interest_unfunded` -- the portion that was neither PAID (serviced
    from pots) nor PAYABLE (capitalized into the balance). A deduction requires
    interest paid or payable; the unfunded is neither (it evaporates from the
    balance sheet), so deducting it was a confident wrong number in the tax
    computation. This also pins the reader: `heloc_interest_unfunded` is
    reported on YearResult and consumed here (it no longer silently evaporates).

    The unfunded path requires the non-reg pot the interest is serviced from to
    be SPENT -- i.e. decumulation (the drawdown liquidates the borrowed pot
    while the HELOC balance remains). Built on a minimal internal config with
    no registered accounts (so the draw lands 100% in non-reg, investment-
    purpose -> margin_proportion 1), capitalize_interest=false (service in
    cash), and a retirement spending_target high enough to exhaust the pot.
    """

    def _config(self):
        # No registered accounts -> fill_room puts the year-0 lump sum entirely
        # in non_reg (investment-purpose, margin_proportion 1). capitalize_
        # interest=false -> the drawn-margin interest is serviced in cash, not
        # capitalized. A high retirement spending_target spends the non-reg pot
        # in decumulation, so the interest becomes unfunded.
        return SimulationConfig(
            projection_years=8,
            investment_return=0.06,
            salary_growth=0.0,
            savings_rate=0.0,
            house_value=800_000,
            mortgage_balance=0,            # mortgage-free
            mortgage_rate=0.05,
            ltv_max=0.80,
            amortization_years=20,
            margin_available=200_000,
            heloc_readvance=False,
            capitalize_interest=False,
            heloc_rate=0.05,
            family_members=[
                {"role": "primary", "gross_income": 120_000,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1955, "retirement_age": 56},
                {"role": "spouse", "gross_income": 0,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1957, "retirement_age": 56},
            ],
            children=[],
            retirement_data={"spending_target": 200_000, "drawdown_order":
                             ["non_reg", "tfsa", "rrsp"]},
        )

    def test_unfunded_interest_is_reported_and_not_deducted(self):
        config = self._config()
        sim = FamilySimulation(config, adapter=CanadaAdapter(config),
                               use_readvanceable=False, deduct_later=False,
                               lump_sum=100_000)
        results = sim.run()
        # There must be a year with a drawn margin and unfunded interest
        # (decumulation spent the non-reg pot the interest is serviced from).
        unfunded_years = [r for r in results if r.heloc_interest_unfunded > 0.0]
        self.assertTrue(unfunded_years,
                        "expected at least one year where the drawn-margin "
                        "interest is unfunded (the pot was spent in "
                        "decumulation); heloc_interest_unfunded is the reader "
                        "that surfaces this (D4 -- it no longer silently "
                        "evaporates)")
        for r in unfunded_years:
            # capitalize_interest=false -> heloc_interest_capitalized=0, so
            # heloc_interest_serviced == the full drawn-margin interest. The
            # deduction must EXCLUDE the unfunded portion (it is neither paid
            # nor payable): margin_deductible_interest == (interest - unfunded)
            # * proportion <= (serviced - unfunded). Deducting the unfunded
            # (the pre-fix bug) would make deductible reach `serviced *
            # proportion`, so asserting deductible <= serviced - unfunded proves
            # the unfunded is excluded (N2 tax fix).
            paid_or_payable = r.heloc_interest_serviced - r.heloc_interest_unfunded
            self.assertLessEqual(
                r.margin_deductible_interest, paid_or_payable + 1e-6,
                f"year {r.year}: margin_deductible_interest "
                f"({r.margin_deductible_interest}) must EXCLUDE the unfunded "
                f"interest (paid_or_payable={paid_or_payable} = serviced "
                f"{r.heloc_interest_serviced} - unfunded "
                f"{r.heloc_interest_unfunded}); deducting the unfunded is the "
                f"N2 tax error.")
            # And it must be strictly less than the full serviced interest when
            # proportion > 0 (the unfunded was deducted pre-fix).
            if r.margin_deductible_interest > 1e-9:
                self.assertLess(
                    r.margin_deductible_interest, r.heloc_interest_serviced,
                    f"year {r.year}: with unfunded > 0 the deduction must be "
                    f"strictly below the full serviced interest")
            # The unfunded amount is reported (the reader), not silently
            # absorbed.
            self.assertGreater(r.heloc_interest_unfunded, 0.0)
