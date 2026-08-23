#!/usr/bin/env python3
"""Enforcement tests for issue #687: the mortgage STRUCTURE (all-mortgage vs.
readvanceable vs. mortgage+revolving-line) as a decision the optimizer sweeps
and ranks, alongside ``refinance_options``/``renewal_options`` and
``decisions.income[]`` (#665).

Before this fix, a household facing this genuinely irreversible structural
choice at a notary appointment had no way to express it in the contract --
``liabilities[]`` states what a household HAS, not what it is CHOOSING
between -- so the question was answered by hand-authoring two separate
contract documents and diffing their outputs. That is exactly the workaround
the project's standing rule forbids (AGENTS.md: "if the contract cannot
express the question, that is a missing feature to file, not a script to
write").

Three layers are tested here (DP#11):

1. ``property_structure.apply_structure_overlay`` in isolation -- the pure
   mechanism that splits ONE registered charge between an amortizing
   mortgage and a revolving line, and both OSFI B-20 refusals (DP#17: both
   sides of the 80% combined threshold and the 65% revolving-only
   threshold).
2. ``input_contract.py``'s mapping -- ``decisions.mortgage.structure_options``
   reaches ``property.structure_options``, and a structure that cannot be
   priced (a revolving component with no declared rate) or that would
   breach either OSFI cap is refused at CONTRACT-LOAD time, not just when a
   sweep happens to apply it.
3. ``scenario_discovery.py``/``optimize.py`` integration -- the structure
   dimension is actually swept and ranked, crossed with every declared
   income scenario, and the engine mechanics already built for #664/#681
   (the readvance mechanism) genuinely activate ONLY for a structure that
   declares itself readvanceable.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based (primary/spouse). No real household's data appears here.
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import input_contract as ic
import optimize
import scenario_discovery as sd
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from charge_limits import ChargeLimitExceededError, charge_limit, heloc_revolving_limit
from property_structure import apply_structure_overlay
from simulation_config import SimulationConfig
from year_result import YearResult
from trajectory_invariants import assert_invariant, run_invariant


# ============================================================================
# Fixtures: a fabricated, valid two-adult contract document (DP#4/DP#15).
# ============================================================================

def _two_gen_doc():
    """The shipped example, trimmed to the two-adults-plus-children
    sub-family the legacy engine can represent (same technique
    tests/test_issue_664_655_charge_limit_and_reamortization.py uses).
    house_value = 650,000; baseline mortgage 340,000 + heloc limit 150,000
    -> combined 490,000 (80% charge = 520,000; 65% revolving-only ceiling
    = 422,500)."""
    with open(ic.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]

    def owner_ids(owner):
        return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}

    doc["accounts"] = [a for a in doc["accounts"] if owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"] if owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"] if owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
    # The shipped example DOES declare structure_options (it is the schema's
    # own worked example of this feature) -- strip it back out so this
    # fixture's default is the ordinary, pre-#687 case (absence), same as
    # every other real contract until a household actually asks this
    # question. Tests that need it present declare it explicitly.
    doc["decisions"]["mortgage"].pop("structure_options", None)
    return doc


ALL_MORTGAGE = {"id": "all_mortgage", "label": "Whole charge as an amortizing mortgage",
                 "revolving_share": 0.0}
READVANCEABLE = {"id": "readvanceable", "label": "Same amount, readvanceable",
                  "revolving_share": 0.0, "readvanceable": True,
                  "revolving_rate": 0.05, "revolving_rate_type": "variable"}
SPLIT_WITH_LINE = {"id": "split_with_line", "label": "Smaller mortgage + revolving line",
                    "revolving_share": 0.30, "revolving_rate": 0.05,
                    "revolving_rate_type": "variable"}


# ============================================================================
# property_structure.apply_structure_overlay -- the mechanism
# ============================================================================

class TestApplyStructureOverlay(unittest.TestCase):
    def _baseline(self, house_value=800_000, mortgage_balance=500_000, margin_available=0.0):
        return {"house_value": house_value, "mortgage_balance": mortgage_balance,
                "margin_available": margin_available}

    def test_identity_when_no_structure_declared(self):
        """revolving_share=None (scenario_discovery's fallback when
        decisions.mortgage.structure_options was never declared) is a
        no-op -- DP#13/DP#32: absence is not an opinion."""
        base = self._baseline(mortgage_balance=340_000, margin_available=150_000)
        result = apply_structure_overlay(base, {"id": "declared", "revolving_share": None})
        self.assertEqual(result, base)
        self.assertIsNot(result, base)  # a copy, never the same object (DP#18)

    def test_all_mortgage_clears_any_pre_existing_revolving_facility(self):
        """Structure A: no revolving component at all -- the undrawn line is
        removed and any heloc facts inherited from the base config are
        cleared, not left as stale $0-labelled facility data (DP#18/#663:
        'no facility' and 'a facility with $0 room' are NOT the same state).

        Issue #851: the DRAWN mortgage (340,000) is carried through unchanged
        -- dropping the revolving facility does not add the household's undrawn
        150,000 of HELOC room to its mortgage debt (that phantom +150,000 was
        the whole finding)."""
        base = self._baseline(mortgage_balance=340_000, margin_available=150_000)
        base["heloc_readvance"] = True
        base["heloc_rate"] = 0.0545
        result = apply_structure_overlay(base, ALL_MORTGAGE)
        self.assertEqual(result["mortgage_balance"], 340_000)
        self.assertNotIn("margin_available", result)
        self.assertNotIn("heloc_readvance", result)
        self.assertNotIn("heloc_rate", result)
        self.assertNotIn("heloc_rate_type", result)

    def test_readvanceable_starts_the_line_at_zero_but_flags_the_facility(self):
        """Structure B: the line's limit starts at $0 (the mortgage
        consumes the charge) but the structure is flagged readvanceable --
        margin_available=0.0 must be an EXPLICIT key (a facility that
        exists with zero room right now), not absent (no facility at
        all), because that is what the readvance mechanism (#664/#681)
        needs to grow it from."""
        base = self._baseline(mortgage_balance=490_000, margin_available=0.0)
        result = apply_structure_overlay(base, READVANCEABLE)
        self.assertEqual(result["mortgage_balance"], 490_000)
        self.assertIn("margin_available", result)
        self.assertEqual(result["margin_available"], 0.0)
        self.assertTrue(result["heloc_readvance"])
        self.assertEqual(result["heloc_rate"], 0.05)
        self.assertEqual(result["heloc_rate_type"], "variable")

    def test_split_structure_carves_the_line_from_the_charge_leaving_the_drawn_position(self):
        """Structure C: revolving_share sets aside a fraction of the SAME
        registered charge as the revolving segment -- the charge total is
        unchanged, only the split.

        Issue #851: the segment (``margin_available``) is UNDRAWN room; the
        DRAWN mortgage is carried through unchanged. On the shipped example
        (340,000 drawn + 150,000 undrawn = 490,000 charge) a 30% share carves a
        147,000 undrawn line and leaves the 340,000 debt exactly where it was --
        it does NOT shave the drawn mortgage down to 343,000 (money the
        household never repaid) as the pre-#851 re-split did."""
        base = self._baseline(mortgage_balance=340_000, margin_available=150_000)
        result = apply_structure_overlay(base, SPLIT_WITH_LINE)
        self.assertAlmostEqual(result["mortgage_balance"], 340_000)
        self.assertAlmostEqual(result["margin_available"], 147_000)  # 30% of the 490,000 charge
        self.assertFalse(result["heloc_readvance"])

    def test_split_preserves_the_drawn_position_regardless_of_the_split(self):
        """Issue #851: the split re-labels the charge, never the drawn debt.
        Whatever the pre-existing drawn/undrawn mix, ``mortgage_balance`` (the
        drawn position) is carried through unchanged and only the revolving
        segment (undrawn room = ``charge * revolving_share``) is re-derived --
        so carving out a line cannot move a dollar of DEBT."""
        base = self._baseline(mortgage_balance=400_000, margin_available=100_000)
        result = apply_structure_overlay(base, SPLIT_WITH_LINE)
        # charge = 500,000; 30% revolving segment = 150,000 undrawn room;
        # the 400,000 drawn mortgage is untouched (pre-#851 it became 350,000).
        self.assertAlmostEqual(result["margin_available"], 150_000)
        self.assertAlmostEqual(result["mortgage_balance"], 400_000)

    def test_undrawn_heloc_room_is_never_counted_as_debt_at_any_share(self):
        """Issue #851's acceptance invariant (the one apply_sourcing_overlay's
        ``test_money_is_conserved_at_every_share`` already holds and this path
        did not): for EVERY revolving_share, the household's total booked DEBT
        equals its real drawn position. The shipped example owes 340,000 drawn
        against a 490,000 charge (150,000 undrawn). No share may book any of
        that undrawn room as debt.

        Booked debt is ``mortgage_balance`` plus whatever the line is drawn
        for; on this no-cash-out path the revolving segment stays UNDRAWN
        (drawn only later via cash_out/readvance), so the drawn HELOC is 0 and
        booked debt is exactly ``mortgage_balance``. Pre-#851 this rose to
        490,000 at share 0.0 (+150,000 phantom) and fell to 149,940 at share
        0.694 (-190,060), changing sign across the sweep."""
        DRAWN = 340_000
        for share in (0.0, 0.1, 0.3, 0.5, 0.694):
            with self.subTest(revolving_share=share):
                structure = {"id": "s", "label": "s", "revolving_share": share,
                             "revolving_rate": 0.05, "revolving_rate_type": "variable"}
                result = apply_structure_overlay(
                    self._baseline(mortgage_balance=DRAWN, margin_available=150_000),
                    structure)
                drawn_heloc = 0.0  # undrawn on the no-cash-out path
                booked_debt = result["mortgage_balance"] + drawn_heloc
                self.assertAlmostEqual(
                    booked_debt, DRAWN,
                    msg=f"share {share}: booked debt {booked_debt:,.0f} != real "
                        f"drawn {DRAWN:,.0f} -- undrawn HELOC room counted as debt")


class TestApplyStructureOverlayChargeRefusal(unittest.TestCase):
    """DP#17: both sides of both OSFI B-20 thresholds (80% combined, 65%
    revolving-only), refused loudly (DP#32) rather than silently clamped."""

    def test_combined_at_exactly_80_percent_is_allowed(self):
        house_value = 800_000  # 80% charge = 640,000
        base = {"house_value": house_value, "mortgage_balance": 640_000, "margin_available": 0.0}
        structure = {"id": "s", "label": "at the line", "revolving_share": 0.0}
        result = apply_structure_overlay(base, structure)
        self.assertEqual(result["mortgage_balance"], 640_000)

    def test_combined_above_80_percent_is_refused_not_clamped(self):
        house_value = 800_000  # 80% charge = 640,000
        base = {"house_value": house_value, "mortgage_balance": 640_001, "margin_available": 0.0}
        structure = {"id": "s", "label": "over the line", "revolving_share": 0.0}
        with pytest.raises(ChargeLimitExceededError):
            apply_structure_overlay(base, structure)

    def test_revolving_segment_at_exactly_65_percent_is_allowed(self):
        house_value = 800_000  # 65% revolving-only ceiling = 520,000
        base = {"house_value": house_value, "mortgage_balance": 100_000, "margin_available": 420_000}
        # total = 520,000; revolving_share=1.0 -> revolving segment = 520,000 exactly
        structure = {"id": "s", "label": "line at the ceiling", "revolving_share": 1.0,
                     "revolving_rate": 0.05, "revolving_rate_type": "variable"}
        result = apply_structure_overlay(base, structure)
        self.assertAlmostEqual(result["margin_available"], 520_000)

    def test_revolving_segment_above_65_percent_is_refused_not_clamped(self):
        house_value = 800_000  # 65% revolving-only ceiling = 520,000
        base = {"house_value": house_value, "mortgage_balance": 100_000, "margin_available": 420_001}
        structure = {"id": "s", "label": "line over the ceiling", "revolving_share": 1.0,
                     "revolving_rate": 0.05, "revolving_rate_type": "variable"}
        with pytest.raises(ChargeLimitExceededError):
            apply_structure_overlay(base, structure)

    def test_revolving_ceiling_binds_even_when_combined_has_room(self):
        """The revolving-only 65% ceiling is independent of the 80%
        combined cap (OSFI B-20) -- a structure can be well within the
        combined limit and still be refused on the revolving-only test."""
        house_value = 800_000  # combined 80% = 640,000; revolving 65% = 520,000
        base = {"house_value": house_value, "mortgage_balance": 20_000, "margin_available": 600_000}
        # total = 620,000 (within the 640,000 combined limit) but a
        # revolving_share of 1.0 would put the ENTIRE 620,000 on the
        # revolving segment -- over the 520,000 revolving-only ceiling.
        structure = {"id": "s", "label": "all-revolving", "revolving_share": 1.0,
                     "revolving_rate": 0.05, "revolving_rate_type": "variable"}
        with pytest.raises(ChargeLimitExceededError):
            apply_structure_overlay(base, structure)

    def test_declared_charge_ltv_limit_override_is_honoured(self):
        """DP#13: a config's own declared charge_ltv_limit is a fallback
        override, not an opinion this function substitutes its own for."""
        base = {"house_value": 800_000, "mortgage_balance": 600_001, "margin_available": 0.0,
                "charge_ltv_limit": 0.75}  # 75% of 800k = 600,000
        structure = {"id": "s", "label": "s", "revolving_share": 0.0}
        with pytest.raises(ChargeLimitExceededError):
            apply_structure_overlay(base, structure)


# ============================================================================
# scenario_discovery.py -- the sweep dimension
# ============================================================================

class TestScenarioDiscoveryStructureDimension(unittest.TestCase):
    def test_absence_returns_a_single_identity_entry(self):
        result = sd.discover_anchors({})["mortgage_structure"]
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["revolving_share"])

    def test_declared_options_pass_through(self):
        cfg = {"property": {"structure_options": [ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE]}}
        result = sd.discover_anchors(cfg)["mortgage_structure"]
        self.assertEqual([r["id"] for r in result],
                          ["all_mortgage", "readvanceable", "split_with_line"])


# ============================================================================
# input_contract.py -- mapping and loud refusal at contract-load time
# ============================================================================

class TestContractMapping(unittest.TestCase):
    def test_absent_structure_options_is_not_mapped(self):
        doc = _two_gen_doc()
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("structure_options", legacy["property"])

    def test_declared_structure_options_are_mapped(self):
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE]
        legacy = ic.to_internal_config(doc)
        ids = [o["id"] for o in legacy["property"]["structure_options"]]
        self.assertEqual(ids, ["all_mortgage", "readvanceable", "split_with_line"])

    def test_revolving_share_without_a_rate_is_refused(self):
        """DP#32/#654: a structure that carves out a revolving segment but
        never prices it must be refused, not silently priced off the
        mortgage rate."""
        doc = _two_gen_doc()
        unpriced = {"id": "unpriced", "label": "unpriced line", "revolving_share": 0.3}
        doc["decisions"]["mortgage"]["structure_options"] = [unpriced]
        with pytest.raises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_readvanceable_at_zero_share_without_a_rate_is_also_refused(self):
        """The same refusal applies even at revolving_share=0.0 when
        readvanceable is set -- the line WILL draw later via readvance
        (#664/#681), so it still needs a price today."""
        doc = _two_gen_doc()
        unpriced = {"id": "unpriced", "label": "unpriced readvanceable",
                    "revolving_share": 0.0, "readvanceable": True}
        doc["decisions"]["mortgage"]["structure_options"] = [unpriced]
        with pytest.raises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_all_mortgage_structure_needs_no_rate(self):
        """The flip side (DP#17): revolving_share=0.0, not readvanceable --
        no revolving component at all, so no rate is required."""
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [ALL_MORTGAGE]
        legacy = ic.to_internal_config(doc)  # must not raise
        self.assertEqual(legacy["property"]["structure_options"][0]["revolving_rate"], None)

    def test_a_structure_that_breaches_the_combined_charge_is_refused_at_load_time(self):
        """DP#32: refused as soon as the contract is loaded -- not only if
        a sweep happens to apply this structure later. Since a structure's
        redistribution never changes the TOTAL secured debt (only its
        split), a combined-cap breach can only come from a baseline total
        that is already over the charge -- the exact case a household with
        NO declared heloc liability (a plain mortgage alone) was never
        checked against before #687 (the pre-existing #664 check only runs
        `if heloc:`). house_value=650,000; 80% charge = 520,000."""
        doc = _two_gen_doc()
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "heloc"]
        for liab in doc["liabilities"]:
            if liab["kind"] == "mortgage":
                liab["balance"]["amount"] = 530_000  # alone > 520,000 charge -- no heloc to trip the old check
        breaching = {"id": "over", "label": "over the charge", "revolving_share": 0.0}
        doc["decisions"]["mortgage"]["structure_options"] = [breaching]
        with pytest.raises(ChargeLimitExceededError):
            ic.to_internal_config(doc)

    def test_a_structure_that_breaches_the_revolving_only_ceiling_is_refused_at_load_time(self):
        """65% revolving-only ceiling = 422,500 (independent of the 80%
        combined cap) -- a structure that would carve MOST of the baseline
        490,000 combined debt into the revolving segment breaches it even
        though the combined total stays under 520,000."""
        doc = _two_gen_doc()
        too_much_revolving = {"id": "too_much", "label": "too much revolving",
                              "revolving_share": 0.9,  # 90% of 490,000 = 441,000 > 422,500
                              "revolving_rate": 0.05, "revolving_rate_type": "variable"}
        doc["decisions"]["mortgage"]["structure_options"] = [too_much_revolving]
        with pytest.raises(ChargeLimitExceededError):
            ic.to_internal_config(doc)

    def test_valid_three_structure_set_loads_cleanly(self):
        """Sanity/regression guard: the exact three-structure shape #687's
        issue body describes loads without any refusal."""
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE]
        ic.to_internal_config(doc)  # must not raise


# ============================================================================
# Schema validation (jsonschema-level, DP#17 both sides)
# ============================================================================

class TestSchemaValidation(unittest.TestCase):
    def test_revolving_share_above_one_is_rejected(self):
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [
            {"id": "x", "label": "x", "revolving_share": 1.5}]
        with pytest.raises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_revolving_share_at_one_is_accepted_by_schema(self):
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [
            {"id": "x", "label": "x", "revolving_share": 1.0,
             "revolving_rate": 0.05, "revolving_rate_type": "variable"}]
        ic.validate_contract(doc)  # must not raise

    def test_unknown_key_on_a_structure_option_is_rejected(self):
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [
            {"id": "x", "label": "x", "revolving_share": 0.0, "bogus_key": 1}]
        with pytest.raises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_structure_options_is_optional(self):
        """A contract that never declares this decision remains valid --
        DP#16, absence is the trigger for 'no sweep'."""
        doc = _two_gen_doc()
        assert "structure_options" not in doc["decisions"]["mortgage"]
        ic.validate_contract(doc)  # must not raise


# ============================================================================
# optimize.py -- the structure is actually swept, ranked, and composed
# with every declared income scenario (issue #665's dimension)
# ============================================================================

class TestOptimizeStructureExploration(unittest.TestCase):
    """A full sweep (3 structures x N income scenarios x every discovered
    strategy) is expensive -- computed ONCE in setUpClass and reused across
    every assertion in this class, rather than re-run per test."""

    @classmethod
    def setUpClass(cls):
        doc = _two_gen_doc()
        doc["decisions"]["mortgage"]["structure_options"] = [ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE]
        cls.cfg = ic.to_internal_config(doc)
        cls.results = optimize.run_mortgage_structure_exploration(cls.cfg)

    def test_three_structures_are_swept(self):
        ids = set(r["structure_id"] for r in self.results)
        self.assertEqual(ids, {"all_mortgage", "readvanceable", "split_with_line"})

    def test_results_are_tagged_with_the_income_scenario_too(self):
        """The structure dimension is CROSSED with every declared income
        scenario (#665), not run only at the base income."""
        income_ids = set(r["income_scenario_id"] for r in self.results)
        self.assertGreaterEqual(len(income_ids), 1)
        for structure_id in ("all_mortgage", "readvanceable", "split_with_line"):
            structure_income_ids = {
                r["income_scenario_id"] for r in self.results if r["structure_id"] == structure_id}
            self.assertEqual(
                structure_income_ids, income_ids,
                f"structure {structure_id!r} must be evaluated under EVERY "
                f"declared income scenario, not a subset")

    def test_all_mortgage_never_discovers_the_readvance_strategy(self):
        """Structure A: no revolving line at all -- the Smith Manoeuvre is
        structurally impossible, so 'readvance_priority' must never appear
        as a candidate strategy for this structure, in ANY income
        scenario."""
        all_mortgage_rows = [r for r in self.results if r["structure_id"] == "all_mortgage"]
        self.assertTrue(all_mortgage_rows)
        self.assertFalse(
            any(r.get("readvanceable_mortgage") for r in all_mortgage_rows),
            "structure 'all_mortgage' (no revolving line) must never run "
            "with use_readvanceable=True -- the readvance mechanism is "
            "structurally impossible without a revolving facility (#687).")

    def test_readvanceable_structure_does_discover_the_readvance_strategy(self):
        """Structure B: flagged readvanceable -- the ONLY structure in
        which the readvance mechanism (#664/#681) can ever run."""
        readvanceable_rows = [r for r in self.results if r["structure_id"] == "readvanceable"]
        self.assertTrue(readvanceable_rows)
        self.assertTrue(
            any(r.get("readvanceable_mortgage") for r in readvanceable_rows),
            "structure 'readvanceable' must have the readvance-priority "
            "strategy actually discovered and run -- it is the ONLY "
            "structure where the mechanism can activate (#687).")

    def test_split_with_line_also_never_discovers_readvance(self):
        """Structure C as declared (readvanceable not set) carves part of
        the charge onto a revolving line but does not flag the facility as
        readvanceable -- readvance stays unavailable, same as structure A,
        unless the household ALSO declares it readvanceable.

        NOTE (declared limitation, not hidden): this optimizer DRAWS a
        declared line in full at year 0 and invests it (#257/#577's
        pre-existing `lump_sum = margin_available + cash_out`), so this
        structure is scored as "same total borrowed, part of it carried on
        the line and invested" -- NOT as "that room kept undrawn as standby
        liquidity." _print_structure_report discloses this inline, above
        the table it biases (DP#32/#585)."""
        split_rows = [r for r in self.results if r["structure_id"] == "split_with_line"]
        self.assertTrue(split_rows)
        self.assertFalse(any(r.get("readvanceable_mortgage") for r in split_rows))

    def test_the_three_structures_rank_differently(self):
        """The whole point of #687: these are genuinely different
        structures, and the engine must not report them as equivalent."""
        winners = optimize.winners_by_structure_scenario(self.results)
        by_structure = {w["structure_id"]: w["net_benefit"] for w in winners
                        if w["income_scenario_id"] == winners[0]["income_scenario_id"]}
        values = set(round(v, 2) for v in by_structure.values())
        self.assertGreater(
            len(values), 1,
            "three structurally different mortgage arrangements against "
            "the same charge produced IDENTICAL net benefit -- the "
            "structure choice is not reaching the simulation (#687).")

    def test_charge_invariant_holds_for_every_structure_every_year(self):
        """The #681 trajectory invariant (total_secured_debt <=
        charge_limit) must still hold for every structure this sweep
        produces -- reusing the SAME enforcement mechanism, not a new one
        (DP#9)."""
        house_value = self.cfg["property"]["house_value"]
        for r in self.results:
            # ``year_by_year`` is the asdict()-serialized form (optimize.py's
            # own JSONL/CSV export shape) -- the invariant checks are written
            # against real YearResult objects, so reconstruct them here
            # (round-trips cleanly: asdict() is exactly YearResult's own
            # field set).
            year_results = [YearResult(**yr) for yr in r["year_by_year"]]
            assert_invariant('total_secured_debt_within_charge_limit',
                              year_results, {'house_value': house_value})
            assert_invariant('heloc_within_revolving_limit',
                              year_results, {'house_value': house_value})


class TestStructureRankingPureLogic(unittest.TestCase):
    """winners_by_structure_scenario / structure_ranking_by_income_scenario
    in isolation, with fabricated rows (DP#11) -- the same split
    winners_by_income_scenario uses so 'does the ranking change under job
    loss' is directly testable, not just observable in printed text."""

    def _row(self, structure_id, structure_label, income_id, income_label,
             net_benefit, ruined=False, engaged=True):
        return {
            'structure_id': structure_id, 'structure_label': structure_label,
            'income_scenario_id': income_id, 'income_scenario_label': income_label,
            'strategy': 'balanced', 'deduct_later': False, 'net_benefit': net_benefit,
            'solvency': {'engaged': engaged, 'ruined': ruined, 'first_ruin_year': 2 if ruined else None},
        }

    def test_ranking_can_change_between_income_scenarios(self):
        rows = [
            self._row('a', 'All-mortgage', 'stay', 'Stay', 9_000_000),
            self._row('b', 'Readvanceable', 'stay', 'Stay', 8_000_000),
            self._row('a', 'All-mortgage', 'job_loss', 'Job loss', 3_000_000),
            self._row('b', 'Readvanceable', 'job_loss', 'Job loss', 5_000_000),
        ]
        winners = optimize.winners_by_structure_scenario(rows)
        ranking = optimize.structure_ranking_by_income_scenario(winners)
        self.assertEqual(ranking['stay'][0]['structure_id'], 'a')
        self.assertEqual(
            ranking['job_loss'][0]['structure_id'], 'b',
            "the winning STRUCTURE must be able to differ between income "
            "scenarios -- a structure optimal at full income and inferior "
            "under job loss is precisely what this feature must surface.")

    def test_a_ruined_structure_row_is_not_printed_as_a_bare_figure(self):
        import io, contextlib
        rows = [
            self._row('a', 'All-mortgage', 'job_loss', 'Job loss', 9_000_000),
            self._row('b', 'Readvanceable', 'job_loss', 'Job loss', 4_431_353, ruined=True),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(rows)
        out = buf.getvalue()
        ruin_line = next(l for l in out.splitlines() if 'Readvanceable' in l)
        self.assertIn('RUIN', ruin_line)
        self.assertNotIn('4,431,353', ruin_line)

    def test_an_unchecked_structure_row_is_marked_inline(self):
        """Issue #733's fix applies to this NEW ranking surface too --
        DP#32 must hold consistently, not just on the income-scenario
        table it was originally found on."""
        import io, contextlib
        rows = [
            self._row('a', 'All-mortgage', 'job_loss', 'Job loss', 9_000_000, engaged=False),
            self._row('b', 'Readvanceable', 'job_loss', 'Job loss', 8_500_000, engaged=False),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(rows)
        out = buf.getvalue()
        for l in out.splitlines():
            if 'All-mortgage' in l or 'Readvanceable' in l:
                self.assertIn('UNCHECKED', l)

    def test_a_line_carrying_structure_discloses_its_draw_fraction(self):
        """DP#32 / model_fidelity (#585): issue #735 fixed the approximation
        this test used to guard (an unconditional, full year-0 draw) -- a
        line-carrying structure is now evaluated at several draw fractions
        and the winning row's OWN drawn fraction must be legible next to
        the figure it produced, not left to be inferred."""
        import io, contextlib
        rows = [
            dict(self._row('a', 'All-mortgage', 'stay', 'Stay', 9_000_000),
                 structure_revolving_share=0.0),
            dict(self._row('c', 'Split with line', 'stay', 'Stay', 8_000_000),
                 structure_revolving_share=0.30, draw_fraction=0.25),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(rows)
        out = buf.getvalue()
        self.assertIn('HOW THE REVOLVING SEGMENT IS MODELLED', out)
        self.assertIn('draw fractions', out)
        self.assertIn('UNDRAWN', out)
        # The line-carrying structure's winning row shows its OWN drawn
        # fraction; the line-free structure (A) shows none at all.
        split_line = next(l for l in out.splitlines() if 'Split with line' in l)
        mortgage_line = next(l for l in out.splitlines() if 'All-mortgage' in l)
        self.assertIn('draw 25%', split_line)
        self.assertNotIn('draw', mortgage_line)

    def test_no_disclosure_when_no_structure_carries_a_line(self):
        """DP#17, the flip side: a sweep of structures that carry NO
        revolving segment at all makes no such claim -- the caveat is
        printed where it applies, not indiscriminately."""
        import io, contextlib
        rows = [
            dict(self._row('a', 'All-mortgage', 'stay', 'Stay', 9_000_000),
                 structure_revolving_share=0.0),
            dict(self._row('b', 'Readvanceable', 'stay', 'Stay', 9_500_000),
                 structure_revolving_share=0.0),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(rows)
        self.assertNotIn('HOW THE REVOLVING SEGMENT IS MODELLED', buf.getvalue())

    def test_a_single_structure_prints_nothing(self):
        """DP#16: no CLI flag, no opt-in -- a household that never declared
        decisions.mortgage.structure_options (the scenario_discovery
        single-'declared'-entry fallback) sees nothing extra."""
        import io, contextlib
        rows = [self._row('declared', 'Declared structure', 'stay', 'Stay', 9_000_000)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(rows)
        self.assertEqual(buf.getvalue(), '')


# ============================================================================
# A real multi-year run stays within the charge for every structure
# (regression lock: the invariant, exercised through the actual engine).
# ============================================================================

class TestRealRunInvariantAcrossStructures(unittest.TestCase):
    def _run(self, structure, house_value=800_000, total_secured=500_000):
        base_property = {"house_value": house_value, "mortgage_balance": total_secured,
                          "margin_available": 0.0}
        applied = apply_structure_overlay(base_property, structure)
        config = SimulationConfig(
            projection_years=10, house_value=house_value,
            mortgage_balance=applied["mortgage_balance"],
            margin_available=applied.get("margin_available", 0.0),
            heloc_readvance=applied.get("heloc_readvance", False),
            heloc_rate=applied.get("heloc_rate"),
            mortgage_rate=0.05, amortization_years=20,
            refinance_amortization_years=20,
            family_members=[{'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
                              'retirement_age': 65, 'rrsp_room_accumulated': 0,
                              'tfsa_room_accumulated': 20_000}],
            start_year=2026, investment_return=0.06, savings_rate=0.10,
        )
        use_readvanceable = bool(structure.get("readvanceable"))
        lump_sum = config.margin_available if use_readvanceable else 0.0
        sim = FamilySimulation(config, adapter=CanadaAdapter(config),
                                use_readvanceable=use_readvanceable,
                                deduct_later=False, lump_sum=lump_sum)
        return sim.run(), house_value

    def test_all_mortgage_never_breaches_the_charge(self):
        results, house_value = self._run(ALL_MORTGAGE)
        assert_invariant('total_secured_debt_within_charge_limit', results, {'house_value': house_value})

    def test_readvanceable_never_breaches_the_charge_even_as_the_line_grows(self):
        """The regression this whole issue is about: as the readvance
        mechanism grows the line from $0 with each year's principal
        paydown, total secured debt must stay inside the SAME charge that
        bounded it before any principal was repaid."""
        results, house_value = self._run(READVANCEABLE)
        assert_invariant('total_secured_debt_within_charge_limit', results, {'house_value': house_value})
        # The mechanism actually ran (not a no-op): some readvance occurred.
        self.assertTrue(any(getattr(r, 'heloc_balance', 0) > 0 for r in results),
                        "the readvanceable structure never drew its line -- "
                        "the readvance mechanism did not activate at all")

    def test_split_with_line_never_breaches_the_charge(self):
        results, house_value = self._run(SPLIT_WITH_LINE)
        assert_invariant('total_secured_debt_within_charge_limit', results, {'house_value': house_value})
        assert_invariant('heloc_within_revolving_limit', results, {'house_value': house_value})


if __name__ == '__main__':
    unittest.main()
