#!/usr/bin/env python3
"""Enforcement tests for issues #845 / #849: the mortgage-STRUCTURE ranking is
scored at the household's DECLARED refinance option(s), and "take the surplus
as a mortgage advance vs. draw it from the revolving line" is rankable
head-to-head at equal leverage.

#845's defect: ``run_mortgage_structure_exploration`` scored every structure at
the CURRENT charge, ignoring ``decisions.mortgage.refinance_options`` -- so an
irreversible, notary-day choice was ranked at a leverage the report does not
recommend, and a structure with no way to source the surplus at cash-out $0 was
partly measured on "cannot execute this plan" rather than on "worse at equal
leverage".

#849's defect, the same cross from the other side: ``cash_out`` can say HOW
MUCH is advanced; nothing in a ``refinance_option`` can say FROM WHERE. At a
FIXED registered charge the tap is ``structure_options[].revolving_share``:

  - 0.0                     -> the whole surplus is an amortizing mortgage
                               advance (cheaper rate; forced principal
                               repayment erodes the deductible balance);
  - >= cash_out / charge     -> the whole surplus is a revolving draw (dearer
                               rate; interest-only, so the deductible balance
                               does not amortize away);
  - in between              -> split, line first.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based. No real household's data appears here.
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import input_contract as ic
import optimize
from charge_limits import ChargeLimitExceededError
from property_structure import apply_sourcing_overlay, apply_structure_overlay
from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_state import margin_draw_for_lump_sum
import contract_schema


# ── Fabricated household (DP#15) ────────────────────────────────────────────
# House 800,000. Existing mortgage 160,000, NO revolving facility yet -- the
# charge is being registered on notary day. 80% LTV charge = 640,000, so the
# surplus is 480,000. The advance is cheaper (3.70%) than the line (4.70%).
HOUSE_VALUE = 800_000.0
EXISTING_MORTGAGE = 160_000.0
CHARGE = 640_000.0
SURPLUS = 480_000.0
ADVANCE_RATE = 0.037
LINE_RATE = 0.047
# The share that puts the WHOLE surplus on the line: 480,000 / 640,000.
ALL_LINE_SHARE = SURPLUS / CHARGE

NO_CASH_OUT = {"id": "no_cash_out", "label": "No refinance", "cash_out": 0,
               "ltv": EXISTING_MORTGAGE / HOUSE_VALUE, "amortization_years": 25}
CASH_OUT_80 = {"id": "cash_out_80", "label": "Register the charge to 80% LTV",
               "cash_out": SURPLUS, "ltv": 0.80, "amortization_years": 25}

ADVANCE = {"id": "advance", "label": "Surplus as a mortgage advance",
           "revolving_share": 0.0}
LINE = {"id": "line", "label": "Surplus drawn from the revolving line",
        "revolving_share": ALL_LINE_SHARE,
        "revolving_rate": LINE_RATE, "revolving_rate_type": "variable"}
HALF = {"id": "half", "label": "Half advance, half line",
        "revolving_share": ALL_LINE_SHARE / 2,
        "revolving_rate": LINE_RATE, "revolving_rate_type": "variable"}


def _post_refinance_property():
    """The property dict AFTER the refinance overlay books the surplus -- the
    exact input ``apply_sourcing_overlay`` is designed to consume."""
    base = {"family": {"members": []},
            "property": {"house_value": HOUSE_VALUE,
                         "mortgage_balance": EXISTING_MORTGAGE,
                         "mortgage_rate": ADVANCE_RATE,
                         "refinance_amortization_years": 25}}
    overlay = ScenarioOverlay(label="80%", cash_out=SURPLUS,
                              mortgage_rate=ADVANCE_RATE, ltv=0.80)
    return apply_overlay(base, overlay)["property"]


def _booked(property_cfg):
    """What the engine will actually book from this property dict at
    draw_fraction 0.0: (mortgage debt, drawn HELOC, invested lump sum).

    Mirrors ``optimizer.py``'s own two rules -- ``lump_sum = margin_available *
    draw_fraction + cash_out`` and ``SimState.initial``'s
    ``margin_draw_for_lump_sum`` -- rather than re-deriving them, so this
    assertion is about the ENGINE's behaviour, not about a restatement of it.
    """
    room = property_cfg.get("margin_available", 0.0)
    lump = room * 0.0 + property_cfg.get("cash_out", 0.0)
    return (property_cfg["mortgage_balance"],
            margin_draw_for_lump_sum(lump, room),
            lump)


# ============================================================================
# apply_sourcing_overlay -- the mechanism (DP#11: in isolation, first)
# ============================================================================

class TestApplySourcingOverlay:
    def test_share_zero_takes_the_whole_surplus_as_a_mortgage_advance(self):
        p = apply_sourcing_overlay(_post_refinance_property(), ADVANCE)
        mortgage, heloc, invested = _booked(p)
        assert mortgage == CHARGE
        assert heloc == 0.0
        assert invested == SURPLUS
        assert "margin_available" not in p, (
            "a structure with no revolving component must leave NO facility "
            "behind (DP#32/#663: 'no facility' and 'a facility with $0 room' "
            "are different states)")

    def test_a_share_that_holds_the_surplus_draws_it_all_from_the_line(self):
        """#849's question, expressible at last: same charge, same borrowed
        total, sourced entirely from the line instead of the advance."""
        p = apply_sourcing_overlay(_post_refinance_property(), LINE)
        mortgage, heloc, invested = _booked(p)
        assert mortgage == pytest.approx(EXISTING_MORTGAGE)
        assert heloc == pytest.approx(SURPLUS)
        assert invested == SURPLUS
        assert p["heloc_rate"] == LINE_RATE, (
            "the line must be priced at the STRUCTURE's own declared revolving "
            "rate -- the whole point is that it is dearer than the advance")

    def test_an_intermediate_share_splits_the_surplus_line_first(self):
        p = apply_sourcing_overlay(_post_refinance_property(), HALF)
        mortgage, heloc, invested = _booked(p)
        assert heloc == pytest.approx(SURPLUS / 2)
        assert mortgage == pytest.approx(EXISTING_MORTGAGE + SURPLUS / 2)
        assert invested == SURPLUS

    @pytest.mark.parametrize("share", [0.0, 0.1, 0.25, ALL_LINE_SHARE / 2,
                                       ALL_LINE_SHARE, 0.6])
    def test_money_is_conserved_at_every_share(self, share):
        """The invariant the naive composition breaks: whatever the split, the
        household owes EXACTLY what it borrowed and invests EXACTLY the
        surplus. No share may conjure or destroy a dollar."""
        structure = {"id": "s", "label": "s", "revolving_share": share,
                     "revolving_rate": LINE_RATE, "revolving_rate_type": "variable"}
        mortgage, heloc, invested = _booked(
            apply_sourcing_overlay(_post_refinance_property(), structure))
        assert mortgage + heloc == pytest.approx(EXISTING_MORTGAGE + SURPLUS)
        assert invested == pytest.approx(SURPLUS)

    def test_the_naive_composition_it_replaces_would_mis_book_the_debt(self):
        """Why this function exists rather than reusing apply_structure_overlay
        (DP#17: the negative case, measured, not asserted from the docstring).

        apply_structure_overlay never DRAWS the line -- it leaves the whole
        revolving segment as undrawn room and carries the drawn mortgage through
        (correct on its own no-cash-out path, #851). Composed onto a property a
        refinance has already advanced ``cash_out`` on, that is wrong for a
        line-carrying structure: the surplus stays booked on the mortgage AND
        the optimizer draws the same ``cash_out`` from the line
        (``lump_sum = margin_available * draw_fraction + cash_out``), investing
        the one borrowed dollar twice. apply_sourcing_overlay instead moves the
        surplus draw ONTO the line (``mortgage_balance -= line_draw``), so the
        household owes exactly what it borrowed at every share."""
        base = {"family": {"members": []},
                "property": {"house_value": HOUSE_VALUE,
                             "mortgage_balance": EXISTING_MORTGAGE,
                             "margin_available": 200_000.0,
                             "heloc_rate": LINE_RATE,
                             "mortgage_rate": ADVANCE_RATE,
                             "refinance_amortization_years": 25}}
        overlay = ScenarioOverlay(label="advance", cash_out=100_000.0,
                                  mortgage_rate=ADVANCE_RATE, ltv=0.325)
        post = apply_overlay(base, overlay)["property"]
        owed = EXISTING_MORTGAGE + 100_000.0  # charge is 360,000 (260k drawn + 100k room)
        # A structure that sources the surplus from the line: its revolving
        # segment (360,000 * 0.5 = 180,000) exceeds the 100,000 to be drawn.
        line_structure = {"id": "line", "label": "Surplus from the line",
                          "revolving_share": 0.5, "revolving_rate": LINE_RATE,
                          "revolving_rate_type": "variable"}

        naive_mortgage, naive_heloc, _ = _booked(apply_structure_overlay(post, line_structure))
        assert naive_mortgage + naive_heloc == pytest.approx(owed + 100_000.0), (
            "premise check: the naive composition leaves the surplus on the "
            "mortgage yet still draws cash_out from the line -- over-booking "
            "the 100,000 cash_out twice")

        mortgage, heloc, invested = _booked(apply_sourcing_overlay(post, line_structure))
        assert mortgage + heloc == pytest.approx(owed)
        assert invested == pytest.approx(100_000.0)

    def test_a_share_over_the_revolving_ceiling_is_refused_not_clamped(self):
        """DP#32/#664: the OSFI B-20 revolving-only ceiling (65% LTV) is
        enforced from the SAME helper apply_structure_overlay uses (DP#9)."""
        over = {"id": "over", "label": "Line over the 65% ceiling",
                "revolving_share": 0.9, "revolving_rate": LINE_RATE,
                "revolving_rate_type": "variable"}
        with pytest.raises(ChargeLimitExceededError, match="revolving-only ceiling"):
            apply_sourcing_overlay(_post_refinance_property(), over)

    def test_no_declared_share_is_the_identity(self):
        """DP#13/DP#32: absence is not an opinion -- the no-structure-declared
        fallback must leave the post-refinance property untouched."""
        post = _post_refinance_property()
        assert apply_sourcing_overlay(post, {"id": "declared"}) == post


# ============================================================================
# The cross: structure_refinance_bases / structure_refinance_cells
# ============================================================================

def _doc(refinance_options, structure_options):
    """The shipped example trimmed to the sub-family the engine represents
    (same technique as tests/test_issue_687_mortgage_structure.py), re-based
    onto this file's fabricated property and decisions."""
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = copy.deepcopy(json.load(f))
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
        if o["account"] in {a["id"] for a in doc["accounts"]}]
    doc["estate"]["life_insurance"] = [i for i in doc["estate"]["life_insurance"]
                                       if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [m for m in doc["assumptions"]["mortality"]
                                       if m["person"] in keep]
    doc["properties"][0]["value"]["amount"] = HOUSE_VALUE
    for liability in doc["liabilities"]:
        if liability["kind"] == "mortgage":
            liability["balance"]["amount"] = EXISTING_MORTGAGE
            liability["rate"] = ADVANCE_RATE
    # No pre-existing revolving facility: the charge is registered on notary day.
    doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "heloc"]
    doc["decisions"]["mortgage"]["refinance_options"] = copy.deepcopy(refinance_options)
    doc["decisions"]["mortgage"]["structure_options"] = copy.deepcopy(structure_options)
    return doc


def _cfg(refinance_options=(NO_CASH_OUT, CASH_OUT_80),
         structure_options=(ADVANCE, LINE)):
    return ic.to_internal_config(_doc(list(refinance_options), list(structure_options)))


class TestStructureRefinanceBases:
    def test_declared_refinance_options_become_the_bases(self):
        """#845: the structures are scored at what the household DECLARED, not
        at whatever charge it happens to carry today."""
        bases = optimize.structure_refinance_bases(_cfg())
        assert [b["id"] for b in bases] == ["no_cash_out", "cash_out_80"]
        assert {b["cash_out"] for b in bases} == {0, SURPLUS}
        assert all(b["source"] == "declared" for b in bases)

    def test_no_declaration_keeps_exactly_the_one_current_charge_basis(self):
        """DP#13: a household that declared no refinance question must not
        sprout a 7-rung generic ladder it never asked for -- and must keep the
        single, cash-out $0 basis it had before this fix."""
        cfg = _cfg()
        cfg["scenarios"]["refinance"] = []
        bases = optimize.structure_refinance_bases(cfg)
        assert len(bases) == 1
        assert bases[0]["cash_out"] == 0.0
        assert bases[0]["ltv"] == pytest.approx(EXISTING_MORTGAGE / HOUSE_VALUE)


class TestStructureRefinanceCells:
    def test_the_cross_is_the_product_of_options_and_structures(self):
        cells = optimize.structure_refinance_cells(_cfg())
        assert [(c["basis"]["id"], c["structure"]["id"]) for c in cells] == [
            ("no_cash_out", "advance"), ("no_cash_out", "line"),
            ("cash_out_80", "advance"), ("cash_out_80", "line")]

    def test_the_refinance_overlay_is_applied_before_the_structure_split(self):
        """#845's actual fix, measured on the composed config: at the declared
        80% option BOTH structures carry the full 640,000 charge -- so they are
        comparable AT that leverage, and structure A is no longer scored on
        "cannot source the surplus here"."""
        cells = {(c["basis"]["id"], c["structure"]["id"]): c
                 for c in optimize.structure_refinance_cells(_cfg())}
        advance = cells[("cash_out_80", "advance")]["cfg"]["property"]
        line = cells[("cash_out_80", "line")]["cfg"]["property"]

        assert _booked(advance) == (CHARGE, 0.0, SURPLUS)
        assert _booked(line)[0] == pytest.approx(EXISTING_MORTGAGE)
        assert _booked(line)[1] == pytest.approx(SURPLUS)
        # Equal leverage: the whole point of the fix.
        assert sum(_booked(advance)[:2]) == pytest.approx(sum(_booked(line)[:2]))

    def test_a_cash_out_of_zero_takes_no_overlay_at_all(self):
        """DP#13: at cash-out $0 there is no surplus to source, so the sourcing
        question does not arise and must not perturb the pre-#845 answer."""
        cells = {(c["basis"]["id"], c["structure"]["id"]): c
                 for c in optimize.structure_refinance_cells(_cfg())}
        cfg = _cfg()
        for sid, structure in (("advance", ADVANCE), ("line", LINE)):
            composed = cells[("no_cash_out", sid)]["cfg"]["property"]
            assert composed == optimize._apply_structure_scenario(
                cfg, structure)["property"]

    def test_a_refused_cell_is_named_with_its_reason_never_dropped(self):
        """DP#32/#681: a cell absent from the ranking must say why, in words --
        silence is indistinguishable from a poor score."""
        over = {"id": "over", "label": "Line over the 65% ceiling",
                "revolving_share": 0.9, "revolving_rate": LINE_RATE,
                "revolving_rate_type": "variable"}
        cells = optimize.structure_refinance_cells(
            _cfg(structure_options=(ADVANCE, over)))
        refused = [c for c in cells if c["refusal"] is not None]
        assert refused, "a 90% revolving share at an 80% charge must be refused"
        for c in refused:
            assert c["cfg"] is None
            assert "revolving-only ceiling" in c["refusal"]
            assert c["basis"]["id"] == "cash_out_80", (
                "the refusal must be scoped to the cell that breached -- the "
                "same structure at cash-out $0 is inside the ceiling")


# ============================================================================
# The exploration: the product is ranked, and every row states its basis
# ============================================================================

class TestExplorationRanksTheProduct:
    @classmethod
    def setup_class(cls):
        cls.results = optimize.run_mortgage_structure_exploration(_cfg())

    def test_every_row_is_tagged_with_the_refinance_option_it_was_scored_at(self):
        assert {r["structure_basis_id"] for r in self.results} == {
            "no_cash_out", "cash_out_80"}
        for r in self.results:
            assert r["structure_basis_source"] == "declared"
            assert r["structure_basis_cash_out"] in (0, SURPLUS)

    def test_both_structures_are_scored_at_the_declared_cash_out(self):
        """#845's acceptance: A/B/C are comparable to each other AT the
        recommended leverage."""
        at_80 = {r["structure_id"] for r in self.results
                 if r["structure_basis_id"] == "cash_out_80"}
        assert at_80 == {"advance", "line"}

    def test_advance_and_line_are_rankable_head_to_head_at_equal_leverage(self):
        """#849's acceptance. Same charge, same borrowed total, same invested
        surplus -- the ONLY difference is where the surplus came from, so the
        two figures differ for exactly one reason."""
        winners = optimize.winners_by_structure_scenario(
            [r for r in self.results if r["structure_basis_id"] == "cash_out_80"])
        by_structure = {w["structure_id"]: w["net_benefit"] for w in winners
                        if w["income_scenario_id"] == winners[0]["income_scenario_id"]}
        assert set(by_structure) == {"advance", "line"}
        assert by_structure["advance"] != by_structure["line"], (
            "sourcing the surplus from a 4.70% interest-only line rather than "
            "a 3.70% amortizing advance produced IDENTICAL net benefit -- the "
            "sourcing decision is not reaching the simulation (#849)")

    def test_the_declared_cash_out_actually_moves_the_structure_ranking(self):
        """The regression pin for #845 itself: mutate the DECLARED leaf and the
        structure ranking's numbers must move. Before the fix they could not --
        the exploration never read refinance_options at all.

        This is the "computed, then discarded" probe #848 described but left
        for a follow-up: it asserts the OUTPUT moves, not merely that the key
        is read."""
        other = dict(CASH_OUT_80, cash_out=SURPLUS / 2, ltv=0.50)
        moved = optimize.run_mortgage_structure_exploration(
            _cfg(refinance_options=(NO_CASH_OUT, other)))

        def _at_80(rows):
            return sorted(r["net_benefit"] for r in rows
                          if r["structure_basis_id"] == "cash_out_80")

        assert _at_80(self.results) != _at_80(moved)

    def test_a_refused_cell_is_skipped_not_scored_and_not_fatal(self):
        """DP#32/#681: a cell the engine refused contributes NO rows -- it must
        not be scored on a fabricated fallback config, and it must not take the
        rest of the cross down with it. The report names it instead
        (TestReportNamesRefusedCells)."""
        over = {"id": "over", "label": "Line over the 65% ceiling",
                "revolving_share": 0.9, "revolving_rate": LINE_RATE,
                "revolving_rate_type": "variable"}
        cfg = _cfg(structure_options=(ADVANCE, over))
        cells = optimize.structure_refinance_cells(cfg)
        refused = [c for c in cells if c["refusal"] is not None]
        assert refused, "premise: this cross must contain a refused cell"

        results = optimize.run_mortgage_structure_exploration(cfg, cells=cells)
        assert results, "the surviving cells must still be scored"
        scored = {(r["structure_basis_id"], r["structure_id"]) for r in results}
        for c in refused:
            assert (c["basis"]["id"], c["structure"]["id"]) not in scored

    def test_the_draw_fraction_is_not_swept_on_a_cash_out_basis(self):
        """It would invest the same borrowed dollar twice (``lump_sum =
        margin_available * draw_fraction + cash_out``). At a fixed charge the
        draw is IMPLIED by the sourcing split -- see apply_sourcing_overlay."""
        at_80 = [r for r in self.results if r["structure_basis_id"] == "cash_out_80"]
        assert at_80
        assert {r["draw_fraction"] for r in at_80} == {0.0}


class TestGoldenHouseholdGateHolds:
    def test_a_household_declaring_no_structure_options_never_enters_the_cross(self):
        """The gate the golden invariant (9709753.139463063) rides on: no
        declared structure_options, no exploration, no cross, no change."""
        doc = _doc([NO_CASH_OUT, CASH_OUT_80], [ADVANCE, LINE])
        doc["decisions"]["mortgage"].pop("structure_options")
        cfg = ic.to_internal_config(doc)
        assert not cfg.get("property", {}).get("structure_options")


# ============================================================================
# The report states its basis loudly, per option (DP#32, #848's style)
# ============================================================================

def _row(basis_id, basis_label, cash_out, ltv, structure_id, structure_label,
         share, net_benefit):
    return {
        "structure_basis_id": basis_id, "structure_basis_label": basis_label,
        "structure_basis_source": "declared", "structure_basis_cash_out": cash_out,
        "structure_basis_ltv": ltv,
        "structure_id": structure_id, "structure_label": structure_label,
        "structure_revolving_share": share, "structure_readvanceable": None,
        "income_scenario_id": "current", "income_scenario_label": "Current income",
        "strategy": "balanced", "deduct_later": False, "net_benefit": net_benefit,
        "solvency": {"engaged": True, "ruined": False}, "draw_fraction": 0.0,
    }


def _cross_rows():
    rows = []
    for basis_id, label, cash_out, ltv in (
            ("no_cash_out", "No refinance", 0.0, 0.20),
            ("cash_out_80", "Register the charge to 80% LTV", SURPLUS, 0.80)):
        rows.append(_row(basis_id, label, cash_out, ltv, "advance",
                         "Surplus as a mortgage advance", 0.0, 100_000.0))
        rows.append(_row(basis_id, label, cash_out, ltv, "line",
                         "Surplus drawn from the line", ALL_LINE_SHARE, 90_000.0))
    return rows


class TestReportStatesItsBasis:
    def test_one_table_per_declared_refinance_option(self, capsys):
        optimize._print_structure_report(_cross_rows())
        out = capsys.readouterr().out
        assert out.count("MORTGAGE STRUCTURE RANKING") == 2

    def test_the_cash_out_table_names_its_option_and_its_dollars(self, capsys):
        optimize._print_structure_report(_cross_rows())
        out = capsys.readouterr().out
        assert "✅ BASIS: your declared refinance option" in out
        assert "'Register the charge to 80% LTV'" in out
        assert "CASH-OUT $480,000" in out
        assert "(LTV 80.0%)" in out

    def test_the_cash_out_table_is_not_mistakable_for_the_ltv_sweep(self, capsys):
        """#845's whole complaint: two tables read side by side as one plan."""
        optimize._print_structure_report(_cross_rows())
        out = capsys.readouterr().out
        assert "This is NOT the LTV-sweep basis" in out
        assert "ranks STRUCTURES at ONE fixed charge" in out

    def test_the_cash_out_table_names_the_advance_vs_line_tap(self, capsys):
        optimize._print_structure_report(_cross_rows())
        out = capsys.readouterr().out
        assert "ADVANCE vs LINE" in out
        assert "revolving_share IS the tap" in out

    def test_the_zero_cash_out_table_keeps_its_own_basis_notice(self, capsys):
        """DP#17, the other side: the cash-out $0 basis must NOT claim to have
        been scored at a leverage it was not."""
        optimize._print_structure_report(_cross_rows())
        out = capsys.readouterr().out
        assert "CASH-OUT $0" in out
        assert "NO cash-out sweep" in out
        assert "'No refinance'" in out

    def test_a_cash_out_table_does_not_claim_a_draw_fraction_sweep(self, capsys):
        """#735's disclosure describes a sweep that does not happen on a
        cash-out basis -- printing it there would be a false statement about
        the model (DP#32)."""
        rows = [r for r in _cross_rows() if r["structure_basis_id"] == "cash_out_80"]
        optimize._print_structure_report(rows)
        out = capsys.readouterr().out
        assert "issue #735" not in out
        assert "The line's draw is NOT swept here" in out
        assert "IMPLIED by the sourcing split" in out

    def test_a_cash_out_row_never_prints_draw_0pct_for_a_drawn_line(self, capsys):
        """DP#32, and the exact opposite of the truth if it did: on a cash-out
        basis ``draw_fraction`` is pinned to 0.0 because the draw is IMPLIED by
        the sourcing split -- the line carrying the surplus is FULLY drawn.
        #735's "(draw 0%)" annotation means "this line won UNDRAWN: standby
        liquidity, not leverage", so printing it here would state the reverse."""
        rows = [r for r in _cross_rows() if r["structure_basis_id"] == "cash_out_80"]
        optimize._print_structure_report(rows)
        out = capsys.readouterr().out
        line_row = next(l for l in out.splitlines()
                        if "Surplus drawn from the line" in l)
        assert "(draw" not in line_row

    def test_a_zero_cash_out_row_still_shows_its_swept_draw_fraction(self, capsys):
        """DP#17, the flip side: where the fraction IS swept (#735), the winning
        row's own drawn fraction must stay legible."""
        rows = [dict(r) for r in _cross_rows()
                if r["structure_basis_id"] == "no_cash_out"]
        for r in rows:
            if r["structure_id"] == "line":
                r["draw_fraction"] = 0.25
        optimize._print_structure_report(rows)
        line_row = next(l for l in capsys.readouterr().out.splitlines()
                        if "Surplus drawn from the line" in l)
        assert "draw 25%" in line_row

    def test_a_zero_cash_out_table_keeps_issue_735s_disclosure(self, capsys):
        rows = [r for r in _cross_rows() if r["structure_basis_id"] == "no_cash_out"]
        optimize._print_structure_report(rows)
        out = capsys.readouterr().out
        assert "HOW THE REVOLVING SEGMENT IS MODELLED (issue #735)" in out

    def test_rows_without_basis_tags_still_print_one_table(self, capsys):
        """DP#32: ``structure_basis_id`` absent means the pre-#845 single
        basis, not a crash -- ``structure_basis_id`` names it explicitly."""
        rows = [dict(r) for r in _cross_rows()
                if r["structure_basis_id"] == "no_cash_out"]
        for r in rows:
            del r["structure_basis_id"]
        assert optimize.structure_basis_id(rows[0]) == "current_charge"
        optimize._print_structure_report(rows)
        assert capsys.readouterr().out.count("MORTGAGE STRUCTURE RANKING") == 1


class TestReportNamesRefusedCells:
    def _cells(self):
        return [
            {"basis": {"id": "cash_out_80", "label": "Register the charge to 80% LTV"},
             "structure": {"id": "over", "label": "Line over the ceiling"},
             "cfg": None, "refusal": "ChargeLimitExceededError: revolving-only ceiling"},
            {"basis": {"id": "no_cash_out", "label": "No refinance"},
             "structure": {"id": "advance", "label": "Advance"},
             "cfg": {}, "refusal": None},
        ]

    def test_a_refused_cell_is_named_with_its_reason(self, capsys):
        optimize._print_structure_report(_cross_rows(), cells=self._cells())
        out = capsys.readouterr().out
        assert "NOT SCORED" in out
        assert "'Line over the ceiling' at 'Register the charge to 80% LTV'" in out
        assert "revolving-only ceiling" in out

    def test_nothing_is_printed_when_nothing_was_refused(self, capsys):
        """DP#17: the notice appears where it applies, not indiscriminately."""
        cells = [c for c in self._cells() if c["refusal"] is None]
        optimize._print_structure_report(_cross_rows(), cells=cells)
        assert "NOT SCORED" not in capsys.readouterr().out

    def test_no_cells_supplied_prints_no_notice(self, capsys):
        optimize._print_structure_report(_cross_rows())
        assert "NOT SCORED" not in capsys.readouterr().out


# ── Issue #850: the ranking must state what it prices re: deductibility ──────
# #845 makes advance-vs-line rankable; #849 asks it *because* of a deductibility
# asymmetry. #850 now PRICES that asymmetry, so the caveat must say deductibility
# is priced (not "unproven"), and must still name the one remaining limitation
# (the fed/QC cap valuation) rather than imply a settled number.

def test_issue_850_caveat_states_deductibility_is_priced_and_names_the_limit():
    from optimize import structure_deductibility_caveat_lines
    text = "\n".join(structure_deductibility_caveat_lines()).lower()
    assert "#850" in text, "must cite the tracking issue"
    assert "deductibility is priced" in text, "must state the asymmetry is now modelled"
    assert "cap" in text and ("understates" in text or "understate" in text), \
        "must still disclose the fed/QC cap limitation"
    assert "unproven" not in text, "deductibility is now priced -- must not say unproven"


def test_issue_850_caveat_is_printed_beside_the_structure_ranking():
    import inspect
    import optimize as _o
    src = ""
    for name in dir(_o):
        fn = getattr(_o, name)
        if not callable(fn):
            continue
        try:
            s = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "MORTGAGE STRUCTURE RANKING" in s:
            src = s
            break
    assert src, "could not locate the structure-ranking printer"
    assert "_print_structure_deductibility_caveat" in src, (
        "the structure ranking must print the #850 caveat beside its numbers"
    )
