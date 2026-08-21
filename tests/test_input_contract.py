#!/usr/bin/env python3
"""Tests for input_contract.py (issue #602, epic #603, Track C Phase 1).

Covers: schema composition (universal + Canada overlay), validation of the
shipped example.json, rejection of invalid documents (unknown key, missing
required key, wrong type, bad enum), and the adapter that maps a validated
new-shape document onto the legacy internal dict SimulationConfig.from_dict
already accepts -- proving the new contract can actually drive today's
engine for the sub-family it can represent (#598's documented Phase 1 limit:
one couple + their direct children; #582, DP#17: the adapter's loud refusal
on extra generations must be shown to actually fire, not merely claimed).

All test data uses fake/synthetic names and round numbers (DP#15).
"""

import copy
import json
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_example():
    with open(ic.EXAMPLE_PATH) as f:
        return json.load(f)


def _two_generation_subset(doc):
    """Trim the shipped 4-generation example down to p1/p2 + their direct
    children -- the sub-family Phase 1's adapter can honestly map onto the
    legacy two-adults-plus-children engine (see input_contract.py's module
    docstring)."""
    doc = copy.deepcopy(doc)
    keep_people = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep_people]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep_people]
    doc["accounts"] = [
        a for a in doc["accounts"]
        if _owner_ids(a["owner"]) <= keep_people
    ]
    doc["liabilities"] = [
        l for l in doc["liabilities"]
        if _owner_ids(l["owner"]) <= keep_people
    ]
    doc["properties"] = [
        p for p in doc["properties"]
        if _owner_ids(p["owner"]) <= keep_people
    ]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep_people
    ]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep_people
    ]
    return doc


def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


class SchemaCompositionTest(unittest.TestCase):

    def test_composed_schema_is_valid_draft_2020_12(self):
        schema = ic.compose_schema()
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_overlay_tightens_account_kind_to_a_closed_enum(self):
        schema = ic.compose_schema()
        self.assertEqual(
            set(schema["$defs"]["account_kind"]["enum"]),
            {"rrsp", "spousal_rrsp", "tfsa", "fhsa", "rrif", "lif", "lira",
             "resp", "non_reg", "dcpp", "dbpp", "lsif"},
        )

    def test_overlay_tightens_province_to_a_closed_enum(self):
        schema = ic.compose_schema()
        self.assertIn("quebec", schema["$defs"]["province"]["enum"])
        self.assertEqual(len(schema["$defs"]["province"]["enum"]), 13)

    def test_overlay_cannot_invent_a_new_root_key(self):
        universal = json.loads(ic.UNIVERSAL_SCHEMA_PATH.read_text())
        bad_overlay = {"properties": {"not_a_real_root_key": {"type": "string"}}}
        with self.assertRaises(ic.ContractValidationError):
            ic.compose_schema(universal, bad_overlay)


class ExampleValidationTest(unittest.TestCase):
    """DP#15: example.json has 4 generations / 8 people, full estate, and no
    personal data (fabricated names, round numbers)."""

    def test_example_validates_with_zero_errors(self):
        doc = _load_example()
        ic.validate_contract(doc)  # raises on any violation

    def test_example_has_four_generations(self):
        doc = _load_example()
        # ggm/ggf (great-grandparents) -> gf/gm (grandparents) -> p1/p2
        # (parents) -> ca/cb (children) is 4 generations.
        ids = {p["id"] for p in doc["people"]}
        self.assertEqual(ids, {"ggm", "ggf", "gf", "gm", "p1", "p2", "ca", "cb"})

    def test_example_has_no_personal_data(self):
        doc = _load_example()
        for p in doc["people"]:
            self.assertIsNone(p["legal_name"])

    def test_example_estate_model_is_populated(self):
        doc = _load_example()
        self.assertTrue(doc["estate"]["life_insurance"])
        self.assertTrue(doc["estate"]["rollover_overrides"])
        # #600's headline finding: spousal rollover must be declinable.
        self.assertFalse(doc["estate"]["rollover_overrides"][0]["spousal_rollover"])

    def test_example_expresses_tfsa_successor_holder_vs_beneficiary(self):
        """#600: a successor holder keeps the TFSA sheltered; a beneficiary
        does not. Both must be independently expressible."""
        doc = _load_example()
        accounts_by_id = {a["id"]: a for a in doc["accounts"]}
        self.assertEqual(accounts_by_id["p1_tfsa"]["successor_holder"], "p2")
        # ggm's TFSA passes to her son gf (not a spouse -- ggf predeceased
        # her), so it's a beneficiary designation, not a successor holder:
        # the shelter ends, unlike p1_tfsa above.
        self.assertEqual(accounts_by_id["ggm_tfsa"]["beneficiary"], "gf")
        self.assertIsNone(accounts_by_id["ggm_tfsa"]["successor_holder"])


class RejectionTest(unittest.TestCase):
    """#596: additionalProperties:false everywhere -- an unknown key, a
    missing required key, or a type error is a load error, never a silent
    drop or default."""

    def _valid(self):
        return _two_generation_subset(_load_example())

    def test_unknown_top_level_key_is_rejected(self):
        doc = self._valid()
        doc["totally_made_up_key"] = 1
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_unknown_key_nested_in_person_is_rejected(self):
        doc = self._valid()
        doc["people"][0]["mebmers_typo"] = 1
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_missing_required_key_is_rejected(self):
        doc = self._valid()
        del doc["as_of"]
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_birth_year_instead_of_birth_date_is_rejected(self):
        """#597: birth_year is gone -- only birth_date (a real date) exists."""
        doc = self._valid()
        del doc["people"][0]["birth_date"]
        doc["people"][0]["birth_year"] = 1980
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_rate_entered_as_percent_instead_of_fraction_is_rejected(self):
        doc = self._valid()
        doc["assumptions"]["inflation"] = 2.5  # should be 0.025
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_bad_account_kind_enum_is_rejected(self):
        doc = self._valid()
        doc["accounts"][0]["kind"] = "not_a_real_kind"
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_bad_province_short_form_is_rejected(self):
        """#595/#596: 'qc' is not valid -- long form only."""
        doc = self._valid()
        doc["jurisdiction"]["province"] = "qc"
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_heloc_without_limit_is_rejected(self):
        """#577/#601: limit is required for revolving liability kinds."""
        doc = self._valid()
        for liab in doc["liabilities"]:
            if liab["kind"] == "heloc":
                del liab["limit"]
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_mortgage_may_be_declared_open(self):
        """#651: openness is a first-class, orthogonal fact — a mortgage term
        may declare `open` alongside its fixed/variable rate_type."""
        doc = self._valid()
        for liab in doc["liabilities"]:
            if liab["kind"] == "mortgage":
                liab["open"] = True
        ic.validate_contract(doc)  # raises on any violation

    def test_open_is_optional_on_mortgage(self):
        """#651/DP#32: absence is the closed default — a mortgage with no
        `open` key is still valid (the whole shipped example omits it)."""
        doc = self._valid()
        for liab in doc["liabilities"]:
            if liab["kind"] == "mortgage":
                self.assertNotIn("open", liab)
        ic.validate_contract(doc)

    def test_open_rejected_on_revolving_kind(self):
        """#651: a HELOC/line has no term to break, so `open` is meaningless
        there — declaring it is a load error, not a silent drop."""
        doc = self._valid()
        for liab in doc["liabilities"]:
            if liab["kind"] == "heloc":
                liab["open"] = True
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_real_dollars_without_base_year_is_rejected(self):
        doc = self._valid()
        doc["dollars"] = "real"
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)


class AdapterTest(unittest.TestCase):
    """#602 Phase 1: the adapter maps a validated new-shape document onto
    the legacy internal dict shape, unmodified SimulationConfig.from_dict
    builds a real SimulationConfig from it -- the new contract can actually
    drive today's engine, for the sub-family it can represent."""

    def setUp(self):
        self.doc = _two_generation_subset(_load_example())
        ic.validate_contract(self.doc)  # sanity: the fixture itself is valid

    def test_adapts_to_a_loadable_legacy_dict(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIsInstance(cfg, SimulationConfig)

    def test_primary_and_spouse_income_round_trip(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        primary = next(m for m in cfg.family_members if m["role"] == "primary")
        spouse = next(m for m in cfg.family_members if m["role"] == "spouse")
        self.assertEqual(primary["gross_income"], 118000)
        self.assertEqual(spouse["gross_income"], 96000)

    def test_registered_balances_round_trip(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        primary = next(m for m in cfg.family_members if m["role"] == "primary")
        self.assertEqual(primary["rrsp_balance"], 210000)
        self.assertEqual(primary["tfsa_balance"], 71000)

    def test_children_round_trip(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(len(cfg.children), 2)
        birth_years = {c["birth_year"] for c in cfg.children}
        self.assertEqual(birth_years, {2010, 2013})

    def test_mortgage_and_heloc_round_trip(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(cfg.mortgage_balance, 340000)
        self.assertEqual(cfg.mortgage_rate, 0.045)
        self.assertEqual(cfg.margin_available, 150000)  # limit, not balance (#577/#601)
        # Issue #654: the HELOC's OWN declared rate must reach the internal
        # shape distinctly from the mortgage's -- the fixture's heloc.rate
        # (0.0545) differs from its mortgage.rate (0.045) by construction,
        # exactly the shape (a cheap legacy mortgage alongside a
        # prime-linked HELOC) the bug silently mispriced.
        self.assertEqual(cfg.heloc_rate, 0.0545)
        self.assertEqual(cfg.heloc_rate_type, "variable")
        self.assertNotEqual(cfg.heloc_rate, cfg.mortgage_rate)

    def test_adapted_config_actually_simulates(self):
        """Not just 'loads without crashing' -- drives a real simulation."""
        from simulation import FamilySimulation
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        sim = FamilySimulation(cfg)
        results = sim.run()
        self.assertGreater(len(results), 0)
        self.assertGreater(results[0].total_family_income, 0)

    def test_liquidate_to_target_maps_to_the_internal_retirement_block(self):
        """Issue #1009: the opt-in die-with-(near)-zero leaf in
        assumptions.retirement.liquidate_to_target must reach the internal
        config's retirement block (the engine reads ret.get(
        'liquidate_to_target') in apply_retirement_income). Absence-safe: a
        document that does not declare it carries nothing (no coercion,
        DP#32), so the golden contract stays byte-identical."""
        # Absent: nothing carried (byte-identical, DP#32).
        legacy = ic.to_internal_config(self.doc)
        self.assertNotIn("liquidate_to_target", legacy["retirement"])
        # Declared true: carried through as a bool.
        doc = copy.deepcopy(self.doc)
        doc["assumptions"]["retirement"]["liquidate_to_target"] = True
        legacy_on = ic.to_internal_config(doc)
        self.assertIs(legacy_on["retirement"]["liquidate_to_target"], True)
        # Declared false: carried through as a bool too (false is a real value).
        doc["assumptions"]["retirement"]["liquidate_to_target"] = False
        legacy_off = ic.to_internal_config(doc)
        self.assertIs(legacy_off["retirement"]["liquidate_to_target"], False)

    def test_four_generation_document_is_loudly_refused_not_silently_truncated(self):
        """DP#17/DP#32: prove the guard actually fires (the near-miss class
        this whole epic is about) -- the four-generation example.json has three
        couples / six adults, and the grandparents draw CPP/OAS/pension. Issue
        #899 (part a) uncaps the compute for ACCUMULATING extra adults, but these
        extra adults DECUMULATE (they draw benefits / are retired), which needs
        the spending-target + mortality model tracked in #901. So the document
        must still raise, never silently drop the retired people and produce a
        plausible-but-wrong answer; #901 is the ticket that flips this test to a
        successful run (a retired-extra decumulation run)."""
        full_doc = _load_example()
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(full_doc)
        msg = str(ctx.exception)
        self.assertIn("ADULT", msg)
        self.assertIn("#901", msg)
        # The refused people are exactly the four grandparents -- the primary
        # couple (p1/p2) and their dependent children (ca/cb) are NOT refused.
        for adult in ("ggm", "ggf", "gf", "gm"):
            self.assertIn(adult, msg)

    def test_oas_disabled_maps_to_zero_with_a_warning_not_silently(self):
        """#592 is an engine-side gap this adapter cannot fully close (see
        module docstring) -- but it must not silently ignore the flag."""
        doc = copy.deepcopy(self.doc)
        doc["assumptions"]["tax_law_overrides"]["oas"]["disabled"] = True
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["retirement"]["oas_annual_max"], 0)


class MultiGenerationBoundaryTest(unittest.TestCase):
    """Issue #698 (Step 8 of #643): the contract boundary admits additional
    DEPENDENT generations (held by the N-child machinery, epic #841 + Step 6)
    while still refusing additional ADULTS the two-slot compute cannot hold
    (the N-adult residual, #706/Step 9). The one-couple golden mapping is
    unchanged (see test_golden_trajectory_581 for the byte-identical invariant)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        ic.validate_contract(self.base)

    def _add_dependent_grandchild(self, doc):
        """A grandchild `gc` -- child of `ca` (an extra generation of pure
        DEPENDENT: no spouse, no benefits, no future salary, no retirement age)
        with a small TFSA of their own. Previously in `extra_generations` and
        refused wholesale; now admissible as a child."""
        doc = copy.deepcopy(doc)
        ca = next(p for p in doc["people"] if p["id"] == "ca")
        ca["relationships"].append({"type": "parent_of", "person": "gc"})
        doc["people"].append({
            "id": "gc", "label": "grandchild", "legal_name": None,
            "birth_date": "2006-03-01", "death_date": None,
            "residency": {"province": "quebec", "since": "2006-03-01"},
            "relationships": [], "incomes": [], "study_periods": [],
            "room": {"rrsp": None, "tfsa": None, "fhsa": None, "resp": None},
        })
        doc["accounts"].append({
            "id": "gc_tfsa", "owner": "gc", "kind": "tfsa",
            "balance": {"amount": 5000, "as_of": "2026-06-30"},
            "acb": None, "holdings": [], "beneficiary": None,
            "successor_holder": None,
        })
        return doc

    def test_one_couple_mapping_is_unchanged(self):
        """The relaxation must not disturb the couple/direct-child mapping: the
        one-couple document still maps to exactly primary+spouse and their two
        direct children (the invariant Step 8 preserves by construction)."""
        legacy = ic.to_internal_config(self.base)
        self.assertEqual(
            {m["role"]: m["id"] for m in legacy["family"]["members"]},
            {"primary": "p1", "spouse": "p2"},
        )
        self.assertEqual(
            {c["id"] for c in legacy["family"]["children"]}, {"ca", "cb"})

    def test_dependent_extra_generation_now_loads_and_runs(self):
        """A grandchild generation that was previously refused wholesale now
        loads: the dependent is present as a child with their own account
        attributed, and the household still simulates end-to-end."""
        doc = self._add_dependent_grandchild(self.base)
        ic.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        children = {c["id"]: c for c in legacy["family"]["children"]}
        # The extra generation composes: gc is a child, its TFSA attributed to
        # it (not dropped, not merged into a parent's pot -- DP#32).
        self.assertIn("gc", children)
        self.assertEqual(children["gc"]["tfsa_balance"], 5000)
        # And the couple mapping is untouched by the extra generation.
        self.assertEqual(
            {m["id"] for m in legacy["family"]["members"]}, {"p1", "p2"})
        # Loads AND runs (the acceptance shape #698 asks for at this boundary).
        from simulation import FamilySimulation
        results = FamilySimulation(SimulationConfig.from_dict(legacy)).run()
        self.assertGreater(len(results), 0)

    def test_extra_adult_with_benefits_is_refused_naming_the_residual(self):
        """An extra ADULT drawing CPP/OAS is still refused loudly -- issue #899
        (part a) admits ACCUMULATING extra adults, but a benefit-drawing adult
        DECUMULATES, and their spending target + mortality lifecycle is the
        modeling gap tracked by #901 (the child seam would silently drop the
        benefits, and the accumulation-only compute has no drawdown for them)."""
        doc = copy.deepcopy(self.base)
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["relationships"].append({"type": "parent_of", "person": "gp"})
        doc["people"].append({
            "id": "gp", "label": "grandparent", "legal_name": None,
            "birth_date": "1955-02-01", "death_date": None,
            "residency": {"province": "quebec", "since": "1955-02-01"},
            "relationships": [], "incomes": [], "study_periods": [],
            "benefits": {"cpp": {"start_date": "2020-02-01",
                                 "monthly_amount": 1000,
                                 "as_of": "2019-01-01"},
                         "oas": {"start_date": "2020-02-01", "defer_months": 0}},
            "room": {"rrsp": None, "tfsa": None, "fhsa": None, "resp": None},
        })
        ic.validate_contract(doc)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("gp", msg)
        self.assertIn("ADULT", msg)
        self.assertIn("#901", msg)


class NeedsAdultComputeTest(unittest.TestCase):
    """Issue #698 (Step 8): _needs_adult_compute is the predicate that decides
    whether an extra person carries an ADULT-only fact the dependent-child seam
    would silently drop. Each fact (spouse pairing / retirement benefit / future
    salary / retirement-age candidacy) must independently trip it; a person with
    none is a plain dependent the child seam holds."""

    def _doc(self, retirement_age=None):
        return {"as_of": "2026-01-01",
                "decisions": {"retirement_age": retirement_age or []}}

    def test_spouse_pairing_needs_adult_compute(self):
        person = {"relationships": [{"type": "spouse_of", "person": "y"}]}
        self.assertTrue(ic._needs_adult_compute(self._doc(), "x", person))

    def test_retirement_benefit_needs_adult_compute(self):
        person = {"relationships": [], "benefits": {"oas": {"defer_months": 0}}}
        self.assertTrue(ic._needs_adult_compute(self._doc(), "x", person))

    def test_future_salary_needs_adult_compute(self):
        """A future-dated employment segment is adult-only -- _map_child keeps
        only today's scalar, so mapping this person as a child drops it."""
        person = {"relationships": [], "benefits": {},
                  "incomes": [{"kind": "employment", "amount": 50000,
                               "from": "2030-01-01", "to": None}]}
        self.assertTrue(ic._needs_adult_compute(self._doc(), "x", person))

    def test_plain_dependent_does_not_need_adult_compute(self):
        """No adult-only fact (a current-salary-only, unpaired dependent) -- the
        child seam holds them, so the boundary must admit, not refuse."""
        person = {"relationships": [], "benefits": {},
                  "incomes": [{"kind": "employment", "amount": 8000,
                               "from": "2020-01-01", "to": None}]}
        doc = self._doc([{"person": "someone_else", "candidate_ages": [65]}])
        self.assertFalse(ic._needs_adult_compute(doc, "x", person))


class AccountKindCoverageTest(unittest.TestCase):
    """Issue #647: every one of the twelve account_kind values must either
    reach the engine or refuse loudly -- no silent balance drop. The
    document's own accounts, and every dollar declared in them, must be
    conserved across the ingestion boundary (#574's money-conservation
    invariant, reintroduced by #647 through this very mapper)."""

    def setUp(self):
        self.doc = _two_generation_subset(_load_example())
        ic.validate_contract(self.doc)

    def _account(self, **overrides):
        """A syntactically valid rrsp-shaped account, overridden per test."""
        base = {
            "id": "synthetic_acct", "owner": "p1", "kind": "rrsp",
            "balance": {"amount": 999999, "as_of": "2026-06-30"},
            "acb": None, "holdings": [], "beneficiary": None, "successor_holder": None,
        }
        base.update(overrides)
        return base

    # ── the $72k the issue is named for ──

    def test_spousal_rrsp_and_fhsa_balances_reach_simstate(self):
        """The exact reproduction from #647: p2's $60,000 spousal_rrsp and
        $12,000 fhsa must land in SimState's opening Canada balances, not
        vanish."""
        from simulation_state import SimState, adult_fhsa_total

        from simulation_state import adult_rrsp_slot

        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        state = SimState.initial(cfg)
        canada = state.jurisdiction_state["canada"]
        # #700/#643 (Steps 2/4): the spousal RRSP and FHSA now live in the
        # per-adult stores (adult_rrsp / adult_fhsa), not the removed flat
        # canada keys. The #647 point is unchanged -- the declared balances must
        # still reach SimState's opening Canada state, not vanish.
        spousal_rrsp = sum(e.get("spousal_as_annuitant", 0)
                           for e in canada["adult_rrsp"].values())
        self.assertEqual(spousal_rrsp, 60000)
        self.assertEqual(adult_fhsa_total(canada), 12000)

    def test_dollar_for_dollar_balance_conservation_into_simstate(self):
        """Money-conservation invariant (#574/#647): every dollar declared
        under accounts[].balance.amount for a kind the engine can represent
        must reach SimState's opening total. lsif is the one declared
        exception -- SimState.total_assets() itself does not fold LSIF into
        net worth at all (a separate, pre-existing gap: LSIF reaches
        legacy['lsif'] and lsif_credit.py's own tax-credit calculation, it
        is simply not summed by this one method) -- not silently dropped,
        just not part of this particular total."""
        from simulation_state import SimState

        declared_total = sum(
            a["balance"]["amount"] for a in self.doc["accounts"] if a["kind"] != "lsif"
        )
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        state = SimState.initial(cfg)
        self.assertAlmostEqual(state.total_assets(), declared_total, places=2)

    # ── the engine-unrepresentable kinds (#643): must refuse, not vanish ──

    def test_rrif_account_is_loudly_refused(self):
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(self._account(id="p1_rrif", kind="rrif"))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_dcpp_account_is_loudly_refused(self):
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(self._account(id="p1_dcpp", kind="dcpp"))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_dbpp_account_is_loudly_refused(self):
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(self._account(id="p1_dbpp", kind="dbpp"))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    # ── per-owner limits the engine's household-singleton pots impose ──

    def test_spousal_rrsp_owned_by_primary_is_loudly_refused(self):
        """The engine's one spousal-RRSP pot is structurally tied to the
        SPOUSE's RRIF minimum -- a spousal RRSP owned by the primary has no
        pot to map into (#643) and must refuse, not silently attach to the
        wrong person or vanish."""
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(self._account(id="p1_spousal_rrsp", kind="spousal_rrsp", owner="p1"))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_fhsa_owned_by_both_spouses_reaches_both_members(self):
        """Issue #700/#643/#704 (Step 4): the engine now holds a PER-ADULT FHSA
        store (simulation_state.adult_fhsa), so two adults' FHSA accounts are no
        longer merged-or-refused. Each adult's opening balance must reach its
        OWN member dict -- the example's p2 (spouse) already owns a $12,000
        FHSA; adding a p1 (primary) FHSA must attribute each to its owner, not
        drop one or blend them into a single pot."""
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(self._account(
            id="p1_fhsa", kind="fhsa", owner="p1",
            balance={"amount": 5000, "as_of": "2026-06-30"},
            fhsa={"opened_date": "2024-01-01", "first_time_buyer_since": "2024-01-01"},
        ))
        cfg = ic.to_internal_config(doc)
        primary = next(m for m in cfg["family"]["members"] if m["role"] == "primary")
        spouse = next(m for m in cfg["family"]["members"] if m["role"] == "spouse")
        self.assertEqual(primary["fhsa_balance"], 5000)
        self.assertEqual(spouse["fhsa_balance"], 12000)

    def test_rrsp_owned_by_a_child_reaches_that_child(self):
        """Issue #841 bite 1: a child is now a first-class savings subject --
        a child-owned RRSP opening balance must land on THAT child's own
        per-member dict (not the parents', not vanished)."""
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(
            self._account(id="ca_rrsp", kind="rrsp", owner="ca",
                          balance={"amount": 4200, "as_of": "2026-06-30"}))
        cfg = ic.to_internal_config(doc)
        ca = next(c for c in cfg["family"]["children"] if c["id"] == "ca")
        self.assertEqual(ca["rrsp_balance"], 4200)
        # The child's balance must be attributed to the child, never merged
        # into a parent's opening RRSP -- p1 keeps only its own example
        # balance (the round-trip test above pins p1's own figure).
        p1 = next(m for m in cfg["family"]["members"] if m["role"] == "primary")
        self.assertNotEqual(p1.get("rrsp_balance"), 4200)

    def test_account_owned_by_an_undeclared_person_is_loudly_refused(self):
        """DP#32: promoting children to savings subjects is not a licence to
        silently absorb a balance owned by NOBODY the document declares -- an
        owner that resolves to no declared person must refuse loudly."""
        doc = copy.deepcopy(self.doc)
        doc["accounts"].append(
            self._account(id="ghost_rrsp", kind="rrsp", owner="nobody_declared"))
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    # ── second-of-a-kind: aggregate correctly, or refuse -- never drop it ──

    def test_second_lsif_purchase_is_loudly_refused(self):
        """#647: a second LSIF purchase used to be silently dropped
        (lsif_accounts[0] only). The engine's LSIF model represents exactly
        one purchase -- refuse rather than guess which one, or blend them."""
        doc = copy.deepcopy(self.doc)
        second = copy.deepcopy(next(a for a in doc["accounts"] if a["kind"] == "lsif"))
        second["id"] = "p2_lsif_second"
        second["owner"] = "p2"
        second["balance"]["amount"] = 999999
        doc["accounts"].append(second)
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_second_lira_with_disagreeing_jurisdiction_is_loudly_refused(self):
        """#647: a second LIRA used to be silently dropped. A second LIRA
        that disagrees with the first on jurisdiction/reference_rate/owner
        birth year cannot be safely blended into the engine's one LIRA pot
        -- refuse rather than silently apply one owner's withdrawal rules
        to both."""
        doc = copy.deepcopy(self.doc)
        second = copy.deepcopy(next(a for a in doc["accounts"] if a["kind"] == "lira"))
        second["id"] = "p1_lira_second"
        second["owner"] = "p1"  # different owner -> different birth year
        second["balance"]["amount"] = 999999
        doc["accounts"].append(second)
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_second_lira_agreeing_on_every_fact_sums_the_balance(self):
        """The positive case: two LIRA accounts for the SAME owner (same
        birth year), same jurisdiction, same reference rate -- nothing
        prevents summing them, and #647 requires the second dollar to
        actually reach the engine rather than vanish."""
        doc = copy.deepcopy(self.doc)
        first = next(a for a in doc["accounts"] if a["kind"] == "lira")
        second = copy.deepcopy(first)
        second["id"] = "p2_lira_second"
        second["balance"]["amount"] = 2000
        doc["accounts"].append(second)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["lira"]["balance"], first["balance"]["amount"] + 2000)

    def test_second_resp_account_composition_is_summed_not_dropped(self):
        """#647: a second RESP account's contribution/CESG/QESI composition
        used to be silently dropped (resp_accounts[0] only) -- both must be
        summed into the one household composition bucket the engine
        tracks."""
        doc = copy.deepcopy(self.doc)
        first = next(a for a in doc["accounts"] if a["kind"] == "resp")
        second = copy.deepcopy(first)
        second["id"] = "second_family_resp"
        second["balance"]["amount"] = 10000
        second["resp"]["contributions_total"] = 8000
        second["resp"]["cesg_received"] = 1600
        second["resp"]["qesi_received"] = 400
        doc["accounts"].append(second)
        legacy = ic.to_internal_config(doc)
        comp = legacy["accounts"]["resp_composition"]
        self.assertEqual(
            comp["total_contributions"],
            first["resp"]["contributions_total"] + 8000,
        )
        self.assertEqual(
            comp["total_cesg_received"],
            first["resp"]["cesg_received"] + 1600,
        )
        self.assertEqual(legacy["accounts"]["resp_current_balance"],
                         first["balance"]["amount"] + 10000)


class EstateFactsReachTheConfigTest(unittest.TestCase):
    """#644. This class used to be ``UnmappedKeysAreHonestlyDeclaredTest``, and
    it asserted the exact OPPOSITE of the truth: that estate.* was unmapped and
    "#600 must stay OPEN". #600 has been closed since PR #640 wired the estate
    namespace for real -- but the test kept passing, because it only ever
    checked membership in a hand-written list of strings that nobody remembered
    to update. A test that asserts something false and stays green is worse than
    no test at all: it is documentation rot with a CI badge.

    Replaced with the claim that is actually true, checked against the mapper's
    real output instead of a list of strings -- so it goes red if the estate
    wiring is ever removed, which is the only thing a guard here is good for.

    (The general form of this check is now mechanical: see
    tests/architecture/test_contract_reachability.py, which measures reachability
    for EVERY contract leaf rather than pinning three hand-picked strings.)
    """

    def test_estate_facts_reach_the_internal_config(self):
        legacy = ic.to_internal_config(_two_generation_subset(_load_example()))
        estate = legacy["estate"]
        # default_spousal_rollover + rollover_overrides[] -> a balance-weighted
        # rolled FRACTION; life_insurance[] -> the tax-free death benefit.
        self.assertIn("registered_rolled_fraction", estate)
        self.assertIn("non_reg_rolled_fraction", estate)
        self.assertIn("life_insurance_death_benefit", estate)

    def test_life_insurance_face_amount_moves_the_death_benefit(self):
        """Not "the key is present" -- the VALUE has to matter."""
        doc = _two_generation_subset(_load_example())
        before = ic.to_internal_config(doc)["estate"]["life_insurance_death_benefit"]
        for policy in doc["estate"]["life_insurance"]:
            policy["face_amount"] += 100_000
        after = ic.to_internal_config(doc)["estate"]["life_insurance_death_benefit"]
        self.assertNotEqual(before, after)


class AuthoredContributionStrategiesTest(unittest.TestCase):
    """#713: decisions.contribution_strategy[] was parsed by the schema and
    dropped by the adapter -- a household's own savings strategies never
    reached the optimizer."""

    def setUp(self):
        self.doc = _two_generation_subset(_load_example())

    def test_authored_strategies_reach_the_internal_config(self):
        legacy = ic.to_internal_config(self.doc)
        declared = {s["id"] for s in self.doc["decisions"]["contribution_strategy"]}
        self.assertEqual(declared, set(legacy["strategies"]))

    def test_allocation_percentages_survive_the_mapping(self):
        legacy = ic.to_internal_config(self.doc)
        first = self.doc["decisions"]["contribution_strategy"][0]
        mapped = legacy["strategies"][first["id"]]
        for pct in ("rrsp_pct", "spousal_rrsp_pct", "tfsa_pct",
                    "fhsa_pct", "resp_pct", "non_reg_pct"):
            self.assertEqual(first["allocation"][pct], mapped[pct])

    def test_use_smith_maps_to_the_readvanceable_mechanism(self):
        """DP#7: the engine models the MECHANISM (prioritize_readvanceable),
        not the brand name ('Smith Manoeuvre')."""
        legacy = ic.to_internal_config(self.doc)
        for strat in self.doc["decisions"]["contribution_strategy"]:
            self.assertEqual(
                strat["use_smith"],
                legacy["strategies"][strat["id"]]["prioritize_readvanceable"],
            )

    def test_bracket_target_reaches_the_key_the_engine_actually_reads(self):
        """The deduct-later rule prices the target off
        SimulationConfig.deduct_later_bracket_target, not off the strategy --
        so a target that only landed in the strategy dict would be 'mapped' and
        still do nothing."""
        legacy = ic.to_internal_config(self.doc)
        declared = {
            s["deduct_later_bracket_target"]
            for s in self.doc["decisions"]["contribution_strategy"]
            if s["deduct_later"] and s["deduct_later_bracket_target"] is not None
        }
        self.assertTrue(declared, "fixture declares no deduct-later target")
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(declared.pop(), cfg.deduct_later_bracket_target)
        self.assertTrue(cfg.should_deduct_later)

    def test_no_strategies_declared_leaves_the_optimizer_on_its_defaults(self):
        """DP#13: an empty contribution_strategy[] is 'no opinion'. It must not
        hand the optimizer an EMPTY search space."""
        doc = copy.deepcopy(self.doc)
        doc["decisions"]["contribution_strategy"] = []
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("strategies", legacy)

    def test_two_conflicting_bracket_targets_are_refused_loudly(self):
        """DP#32: the engine has ONE bracket-fill target. Two strategies asking
        for different ones cannot both be honoured -- and silently picking one
        is precisely the defect this module exists to prevent."""
        doc = copy.deepcopy(self.doc)
        strategies = doc["decisions"]["contribution_strategy"]
        template = copy.deepcopy(strategies[0])
        template["id"] = "other_deduct_later"
        template["deduct_later"] = True
        template["deduct_later_bracket_target"] = 55000
        strategies.append(template)
        for s in strategies:
            if s["id"] != "other_deduct_later" and s["deduct_later"]:
                s["deduct_later_bracket_target"] = 117045
        ic.validate_contract(doc)  # the CONTRACT is legal; the ambiguity is ours to refuse

        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)


class TuitionMappingTest(unittest.TestCase):
    """Issue #764: the adapter expands a study_period's `tuition` into a
    per-calendar-year map on the taxed member, warns loudly on the path it
    cannot yet fully credit (a CHILD's own credit -- the transfer mechanism
    is unmodelled), and never silently drops a declared tuition (DP#17/DP#32).
    Issue #783: the Quebec PROVINCIAL tuition credit is now modelled, so the
    pre-#783 QC-provincial-unmodelled warning is removed."""

    def setUp(self):
        self.doc = _two_generation_subset(_load_example())

    def _study(self, start, end, tuition, transfer_to=None):
        sp = {"institution": "Synthetic College", "program": "Round Numbers",
              "start_date": start, "end_date": end, "tuition": tuition}
        if transfer_to is not None:
            sp["transfer_to"] = transfer_to
        return sp

    def _members(self, doc):
        return ic.to_internal_config(doc)["family"]["members"]

    def _child_members(self, doc):
        return ic.to_internal_config(doc)["family"]["children"]

    def test_spouse_tuition_expands_to_a_year_by_year_map(self):
        # A two-year study period pays the SAME eligible tuition in each
        # calendar year it covers (2026 and 2027), keyed by year.
        doc = copy.deepcopy(self.doc)
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["study_periods"] = [self._study("2026-09-01", "2027-05-31", 8000)]
        spouse = next(m for m in self._members(doc) if m["role"] == "spouse")
        self.assertEqual(spouse["tuition_by_year"], {2026: 8000.0, 2027: 8000.0})

    def test_null_end_date_is_a_single_known_year_not_infinity(self):
        # end_date null (ongoing/unknown) pays in the start_year ONLY --
        # annualising to infinity would fabricate tuition (see docstring).
        doc = copy.deepcopy(self.doc)
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["study_periods"] = [self._study("2026-09-01", None, 5000)]
        spouse = next(m for m in self._members(doc) if m["role"] == "spouse")
        self.assertEqual(spouse["tuition_by_year"], {2026: 5000.0})

    def test_no_tuition_declared_leaves_the_member_without_the_key(self):
        # The shipped fixture declares no member tuition -> no tuition_by_year
        # key at all (absence, not a defaulted empty map -- DP#32).
        spouse = next(m for m in self._members(self.doc) if m["role"] == "spouse")
        self.assertNotIn("tuition_by_year", spouse)

    def test_quebec_tuition_no_longer_warns_provincial_unmodelled(self):
        # Issue #783: the Quebec PROVINCIAL tuition credit (TP-1 Schedule T,
        # 8%) is now modelled, so the pre-#783 "only the federal portion is
        # applied" warning is REMOVED. A Quebec spouse's declared tuition no
        # longer fires the QC-provincial-unmodelled warning -- the credit is
        # applied, not warned about (DP#32: the absence is no longer a defect).
        doc = copy.deepcopy(self.doc)
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["study_periods"] = [self._study("2026-09-01", "2026-12-31", 3000)]
        with self.assertNoLogs("input_contract", level="WARNING"):
            self._members(doc)

    def test_child_tuition_is_recorded_for_transfer_not_warned(self):
        # Issue #785: a child's tuition is now TRANSFERRED to a supporting
        # parent/spouse (ITA s.118.8, $5,000 limit), not just recorded-
        # and-warned. The pre-#785 child-tuition warning is REMOVED.
        doc = copy.deepcopy(self.doc)
        ca = next(p for p in doc["people"] if p["id"] == "ca")
        ca["study_periods"] = [self._study("2026-09-01", "2026-12-31", 2000)]
        # No warning fires (transfer is now modelled).
        with self.assertNoLogs("input_contract", level="WARNING"):
            children = self._child_members(doc)
        child = next(c for c in children if c.get("tuition_by_year"))
        self.assertEqual(child["tuition_by_year"], {2026: 2000.0})

    def test_child_transfer_to_is_extracted_onto_the_child(self):
        # Issue #785: a child's study_period declaring `transfer_to` (the
        # supporting parent's person_id) is parsed onto the child config as
        # `tuition_transfer_to`, so the simulation prologue can resolve it to
        # the right taxed member. Exercises input_contract._tuition_transfer_to
        # and the child extraction path (DP#15: fabricated round numbers).
        doc = copy.deepcopy(self.doc)
        ca = next(p for p in doc["people"] if p["id"] == "ca")
        ca["study_periods"] = [self._study("2026-09-01", "2026-12-31", 3000,
                                            transfer_to="p1")]
        children = self._child_members(doc)
        child = next(c for c in children if c.get("tuition_by_year"))
        self.assertEqual(child["tuition_by_year"], {2026: 3000.0})
        self.assertEqual(child["tuition_transfer_to"], "p1")

    def test_taxed_member_transfer_to_is_extracted_onto_the_member(self):
        # Issue #785: a taxed member's (the spouse's) study_period declaring
        # `transfer_to` is parsed onto the member config as
        # `tuition_transfer_to`, and the member carries its `id` so the
        # prologue can resolve a transfer TARGETED at it. Exercises
        # _tuition_transfer_to and the taxed-member extraction + `id` path.
        doc = copy.deepcopy(self.doc)
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["study_periods"] = [self._study("2026-09-01", "2026-12-31", 4000,
                                            transfer_to="p1")]
        members = self._members(doc)
        spouse = next(m for m in members if m["role"] == "spouse")
        self.assertEqual(spouse["tuition_by_year"], {2026: 4000.0})
        self.assertEqual(spouse["tuition_transfer_to"], "p1")
        # The member's own id is carried so a transfer TO it resolves.
        self.assertEqual(spouse["id"], "p2")
        primary = next(m for m in members if m["role"] == "primary")
        self.assertEqual(primary["id"], "p1")

    def test_no_transfer_declared_leaves_no_transfer_key(self):
        # Issue #785: a study_period with tuition but NO `transfer_to` parses
        # to a child with `tuition_by_year` but NO `tuition_transfer_to` key
        # (absence, not a defaulted empty -- DP#32). The credit carries
        # forward, never silently lost.
        doc = copy.deepcopy(self.doc)
        ca = next(p for p in doc["people"] if p["id"] == "ca")
        ca["study_periods"] = [self._study("2026-09-01", "2026-12-31", 2000)]
        children = self._child_members(doc)
        child = next(c for c in children if c.get("tuition_by_year"))
        self.assertNotIn("tuition_transfer_to", child)


if __name__ == "__main__":
    unittest.main()
