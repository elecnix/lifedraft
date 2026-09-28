"""Issue #386: a sweep row's drawdown facts must NAME the candidate they describe.

``sweep._row`` used to run the optimizer for a swept value and publish
``results[0]``'s drawdown summary -- the objective's ARGMAX -- as though it
described the swept axis. An argmax need not vary monotonically in the swept
value: adjacent values can select different winners, so the published
``first_shortfall_year``/``shortfall_years`` sequence could move in either
direction and nothing in the row said which candidate it quoted. Two families of
facts (the solvency rule's, and the drawdown rule's) then disagreed about the same
rows with no way for a reader to tell which one was a frontier.

Every row now carries BOTH families, each keyed by the candidate it came from:

* ``plan_*`` -- the PASS-1 candidate the optimizer ranks first on the contract
  **as declared**, held constant across the sweep (DP#5: anchor the decision,
  overlay the sensitivity). The decision is frozen, so these facts move with the
  swept axis alone -- they are the frontier a reader locates.
* ``winner_*`` -- the objective's argmax at THAT value, with its identity, so a
  reader can see when adjacent rows stopped describing the same plan.

The division of proof in this module is deliberate (DP#11):

* the SEPARATION itself -- which candidate each family is read off, the anchor
  being pass 1 rather than the overall winner, an absent plan staying absent --
  is proven against canned ``run_optimization`` output. That is a unit test of
  this module's own lookup, and it is deterministic: it does not depend on the
  engine happening to pick different winners today.
* the FRONTIER -- that the frozen plan's exhaustion does not get better as the
  spending target rises -- is proven against the real engine on the synthetic
  tightening this issue reports (issue #386's proposed option 3).

All figures fabricated, role-based ids (DP#4/#DP#15).
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sweep
import contract_schema
from objective import MIN_AFTER_TAX_ESTATE

_SPEND = "assumptions.retirement.spending_target"
_EXAMPLE_INPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")


def _couple_doc() -> dict:
    """The shipped schema example trimmed to the couple + their children -- the
    shape the Phase-1 engine can actually simulate (#598; the full
    four-generation example is correctly REFUSED). Mirrors
    ``test_issue_771_generalized_sweeps._couple_doc``."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    keep = {"p1", "p2", "ca", "cb"}

    def owner_ids(owner):
        return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}

    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    for coll in ("accounts", "liabilities", "properties"):
        doc[coll] = [x for x in doc[coll] if owner_ids(x["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep
    ]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep
    ]
    doc.pop("provenance", None)
    return doc


def _tightened_doc() -> dict:
    """The shipped example tightened the synthetic way issue #386 reports: no
    employment income, asset balances scaled to 15% (accounts the household does
    not draw from zeroed), and the ``decisions.income`` scenarios that override a
    now-absent income dropped. A poor-enough household that the retirement
    drawdown actually exhausts, so the frontier is measurable."""
    with open(_EXAMPLE_INPUT) as fh:
        doc = json.load(fh)
    for person in doc["people"]:
        person["incomes"] = []
    keep = {"p1_rrsp", "p1_tfsa", "joint_nonreg", "p2_rrsp", "p2_tfsa",
            "p2_lira", "spousal_rrsp_p2"}
    for account in doc["accounts"]:
        balance = account.get("balance")
        if isinstance(balance, dict) and isinstance(balance.get("amount"), (int, float)):
            balance["amount"] = (round(balance["amount"] * 0.15, 2)
                                 if account.get("label") in keep else 0.0)
    decisions = doc.get("decisions", {})
    if "income" in decisions:  # drop scenarios overriding a now-absent income
        decisions["income"] = [s for s in decisions["income"] if not s.get("overrides")]
    return doc


# ── Canned optimizer output: the separation, deterministically ───────────────

def _candidate(strategy, order_id, score, first_year, shortfall_years):
    """One ranked result dict the way ``run_optimization`` shapes it."""
    return {
        "strategy": strategy,
        "drawdown_order_id": order_id,
        "draw_fraction": 0.0,
        "label": None,
        "objective_score": score,
        "net_benefit": score,
        "drawdown_shortfall": {
            "engaged": True,
            "exhausted": first_year is not None,
            "first_shortfall_year": first_year,
            "first_shortfall_gap": 0.0,
            "shortfall_years": shortfall_years,
            "total_unmet": 0.0,
        },
    }


def _fake_optimizer(per_spend, calls=None):
    """A ``run_optimization`` stand-in keyed on the spending target the swept
    copy carries -- so the anchor run (the contract AS DECLARED) and each swept
    value can be given different rankings, exactly as the engine can."""
    def fake(cfg, *args, **kwargs):
        spend = cfg["retirement"]["spending_target"]
        if calls is not None:
            calls.append((spend, kwargs.get("objective")))
        return per_spend[spend]
    return fake


# The declared value of ``schema/example.json`` -- the anchor run's key.
_DECLARED = 90000

# A ranked pair where the PASS-1 row (``configured`` order: the household's own
# declared drawdown order) and a #618 PASS-2 order variant trade places. The
# anchor is the pass-1 row whatever the ranking says.
_PER_SPEND = {
    _DECLARED: [_candidate("anchored + RRSP-meltdown", "rrsp_meltdown", 500.0, 30, 0),
                _candidate("anchored", "configured", 400.0, 20, 2)],
    100000: [_candidate("anchored + RRSP-meltdown", "rrsp_meltdown", 900.0, 25, 0),
             _candidate("anchored", "configured", 800.0, 19, 3)],
    140000: [_candidate("anchored", "configured", 600.0, 17, 5),
             _candidate("anchored + RRSP-meltdown", "rrsp_meltdown", 100.0, 4, 40)],
}


def _rows(monkeypatch, values, per_spend=None, calls=None):
    monkeypatch.setattr(sweep, "run_optimization",
                        _fake_optimizer(per_spend or _PER_SPEND, calls=calls))
    return sweep.run_axis_sweep(_couple_doc(), _SPEND, values)


def test_the_plan_family_is_read_off_the_anchored_candidate_not_the_winner(monkeypatch):
    """At 100000 the argmax is the pass-2 variant and the frozen plan is the
    pass-1 row: the ``plan_*`` facts must be the pass-1 row's (800 / yr 19 / 3
    shortfall years), NOT the winner's (900 / yr 25 / 0)."""
    rows = _rows(monkeypatch, [100000, 140000])

    assert rows[0]["winner_strategy"] == "anchored + RRSP-meltdown"
    assert rows[0]["winner_objective_score"] == 900.0
    assert rows[0]["winner_first_shortfall_year"] == 25
    assert rows[0]["winner_shortfall_years"] == 0

    assert rows[0]["plan_strategy"] == "anchored"
    assert rows[0]["plan_drawdown_order_id"] == "configured"
    assert rows[0]["plan_objective_score"] == 800.0
    assert rows[0]["plan_first_shortfall_year"] == 19
    assert rows[0]["plan_shortfall_years"] == 3
    assert rows[0]["plan_exhausted"] is True


def test_the_plan_is_frozen_while_the_winner_moves_with_the_ranking(monkeypatch):
    """The load-bearing #386 property: across values the plan column quotes the
    SAME decision, while the winner column follows the argmax -- which switches
    between the two rows when the ranking does."""
    first, second = _rows(monkeypatch, [100000, 140000])

    assert (first["plan_strategy"], first["plan_drawdown_order_id"],
            first["plan_draw_fraction"]) == \
           (second["plan_strategy"], second["plan_drawdown_order_id"],
            second["plan_draw_fraction"])
    assert first["winner_strategy"] != second["winner_strategy"]
    # ... and where the argmax IS the plan, the two families agree.
    assert second["winner_strategy"] == second["plan_strategy"]
    assert second["winner_objective_score"] == second["plan_objective_score"]
    assert second["winner_first_shortfall_year"] == second["plan_first_shortfall_year"]


def test_the_anchor_is_the_pass_one_row_not_the_overall_declared_winner(monkeypatch):
    """The declared contract's own argmax is a #618 pass-2 variant. Anchoring on
    it would make the plan an artifact of the drawdown search -- and would make
    the plan VANISH at every value where a different accumulation strategy wins
    pass 1, since pass 2 never runs for the losers. The anchor is therefore the
    top-ranked PASS-1 row (``configured`` = the household's declared order)."""
    rows = _rows(monkeypatch, [100000, 140000])

    assert all(r["plan_strategy"] == "anchored" for r in rows)
    assert all(r["plan_drawdown_order_id"] == "configured" for r in rows)
    assert all(r["plan_present"] for r in rows)


def test_a_plan_the_value_never_ranked_is_ABSENT_not_substituted(monkeypatch):
    """A value whose ranking lacks the anchored candidate leaves the plan ABSENT
    (DP#32) -- the winner is never quietly promoted into the plan column. The
    plan's identity is still named, so the row says WHICH plan it could not
    find."""
    per_spend = dict(_PER_SPEND)
    per_spend[140000] = [_candidate("anchored + RRSP-meltdown", "rrsp_meltdown", 100.0, 4, 40)]
    rows = _rows(monkeypatch, [100000, 140000], per_spend=per_spend)

    absent = rows[1]
    assert absent["plan_present"] is False
    assert absent["plan_strategy"] == "anchored"      # named, though not found
    assert absent["plan_objective_score"] is None
    assert absent["plan_engaged"] is False            # "not checked", never a zero
    assert absent["plan_first_shortfall_year"] is None
    assert absent["plan_shortfall_years"] == 0
    # The winner is still reported -- the absence is scoped to the plan.
    assert absent["winner_present"] is True
    assert absent["winner_strategy"] == "anchored + RRSP-meltdown"
    # ... and the present value's plan is untouched.
    assert rows[0]["plan_present"] is True


def test_the_plan_column_does_not_depend_on_the_value_order(monkeypatch):
    """The anchor is a property of the contract, not of the first value in the
    list: reordering ``values`` cannot move the frozen decision."""
    forward = _rows(monkeypatch, [100000, 140000])
    backward = _rows(monkeypatch, [140000, 100000])
    by_value = {r["value"]: r for r in backward}
    for row in forward:
        other = by_value[row["value"]]
        assert row["plan_objective_score"] == other["plan_objective_score"]
        assert row["plan_first_shortfall_year"] == other["plan_first_shortfall_year"]
        assert row["plan_shortfall_years"] == other["plan_shortfall_years"]
        assert row["plan_exhausted"] == other["plan_exhausted"]


def test_the_anchor_run_uses_the_callers_objective(monkeypatch):
    """DP#22: the frozen plan is the decision the optimizer reaches under the
    objective the caller asked for -- the anchor pass must not silently score
    under a default."""
    calls = []
    monkeypatch.setattr(sweep, "run_optimization",
                        _fake_optimizer(_PER_SPEND, calls=calls))
    sweep.run_axis_sweep(_couple_doc(), _SPEND, [100000, 140000],
                         objective=MIN_AFTER_TAX_ESTATE)
    assert calls
    assert all(objective is MIN_AFTER_TAX_ESTATE for _spend, objective in calls)


def test_run_sweeps_pays_the_anchor_pass_once_for_the_whole_report(monkeypatch):
    """The anchor depends on the contract and the objective, never on the axis:
    a report of two axes must not re-run the declared contract per axis."""
    doc = _couple_doc()
    doc["sensitivity"]["sweeps"] = {_SPEND: [100000, 140000], "savings_rate": [0.2]}
    calls = []
    monkeypatch.setattr(sweep, "run_optimization",
                        _fake_optimizer(_PER_SPEND, calls=calls))
    report = sweep.run_sweeps(doc)
    assert set(report) == {_SPEND, "savings_rate"}
    assert len(calls) == 3 + 1  # two swept values + one savings value + one anchor


def test_run_sweeps_validates_every_axis_before_any_optimizer_pass(monkeypatch):
    """A typo in the SECOND declared axis must cost no optimizer pass at all --
    the anchor cannot be burned before the sweep is known to be resolvable
    (DP#32)."""
    doc = _couple_doc()
    doc["sensitivity"]["sweeps"] = {_SPEND: [100000], "assumptions.not.a.leaf": [1]}
    called = []
    monkeypatch.setattr(sweep, "run_optimization", lambda *a, **k: called.append(1) or [])
    with pytest.raises(sweep.SweepPathError) as exc:
        sweep.run_sweeps(doc)
    assert "assumptions.not.a.leaf" in str(exc.value)
    assert called == []


# ── The frontier: the FROZEN plan's exhaustion is monotone in the axis ───────

def test_the_frozen_plans_exhaustion_is_monotone_in_the_retirement_target():
    """Issue #386's proposed invariant, on the real engine: with the decision
    frozen, a HIGHER retirement spending target cannot exhaust LATER, cannot
    shorten the shortfall, and cannot un-exhaust a plan that already ran dry.

    Read on the ``plan_*`` family only. This is the property that makes a sweep
    table a frontier ("which spend level is still fundable"); the ``winner_*``
    family is an argmax and is not required to have it."""
    doc = _tightened_doc()
    spends = [10000, 20000, 40000]
    rows = sweep.run_axis_sweep(doc, _SPEND, spends)

    # The anchor survives every value here; if it stops doing so the assertions
    # below would be vacuous, so assert presence explicitly.
    assert [r["plan_present"] for r in rows] == [True, True, True]
    assert all(r["plan_exhausted"] for r in rows)

    years = [r["plan_shortfall_years"] for r in rows]
    assert all(years[i] <= years[i + 1] for i in range(len(years) - 1)), (
        f"a higher spending target must not SHORTEN the frozen plan's shortfall; "
        f"got {years}"
    )
    assert years[0] < years[-1]  # the axis is engaged: the column actually moves

    first_years = [r["plan_first_shortfall_year"] for r in rows]
    assert all(y is not None for y in first_years)
    assert all(first_years[i] >= first_years[i + 1]
               for i in range(len(first_years) - 1)), (
        f"a higher spending target must not push the frozen plan's first "
        f"shortfall LATER; got {first_years}"
    )


def test_the_engine_row_names_the_candidate_each_family_was_read_off():
    """On a real contract, every row carries both identities and both presence
    flags -- the disclosure #386 asks for is on the artifact, not only in the
    engine's internals."""
    doc = _tightened_doc()
    rows = sweep.run_axis_sweep(doc, _SPEND, [10000, 40000])
    for row in rows:
        assert row["plan_present"] is True
        assert row["winner_present"] is True
        assert row["plan_strategy"]            # the frozen decision is named
        assert row["winner_strategy"]          # ... and so is the argmax
        for family in ("plan", "winner"):
            for field in ("objective_score", "engaged", "exhausted",
                          "first_shortfall_year", "shortfall_years"):
                assert f"{family}_{field}" in row
